"""
FlipHTML5 Downloader (Enhanced Edition)
Downloads books from fliphtml5.com and converts them to PDF.
Supports both plaintext and encrypted (WASM) page structures.
Features multi-threaded fast downloading, robust TLS connections, and automatic input sanitization.
"""

import os
import re
import sys
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import io

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from urllib3.util.ssl_ import create_urllib3_context
import img2pdf
from PIL import Image

# Embedded decryption script for Deno
DECRYPT_JS_CONTENT = r"""// decrypt.js: Decrypts fliphtml5_pages string passed via stdin
const res = await fetch('https://static.fliphtml5.com/resourceFiles/html5_templates/js/deString.js');
let code = await res.text();
globalThis.process = undefined;
globalThis.window = globalThis;
globalThis.location = { href: 'https://static.fliphtml5.com/' };

const fn = new Function('Module', code + '; return Module;');
const Module = {};
fn(Module);

function deString(str) {
    const encoder = new TextEncoder();
    const encoded = encoder.encode(str);
    const ptr = Module._malloc(encoded.length + 1);
    Module.HEAPU8.set(encoded, ptr);
    Module.HEAPU8[ptr + encoded.length] = 0;
    
    const outPtr = Module._DeString(ptr);
    let end = outPtr;
    while (Module.HEAPU8[end] !== 0) end++;
    const decoder = new TextDecoder();
    const result = decoder.decode(Module.HEAPU8.subarray(outPtr, end));
    Module._FreeMemory(outPtr);
    Module._free(ptr);
    return result;
}

const decoder = new TextDecoder();
let inputData = "";
for await (const chunk of Deno.stdin.readable) {
    inputData += decoder.decode(chunk, { stream: true });
}
inputData += decoder.decode();
const raw = inputData.trim();

if (!raw) {
    console.error("No input received via stdin");
    Deno.exit(1);
}

setTimeout(() => {
    try {
        const decrypted = deString(raw);
        const lastBracket = decrypted.lastIndexOf(']');
        if (lastBracket === -1) {
            console.error("Could not find closing bracket in decrypted data");
            Deno.exit(1);
        }
        console.log(decrypted.substring(0, lastBracket + 1));
        Deno.exit(0);
    } catch(e) {
        console.error("Decryption error:", e);
        Deno.exit(1);
    }
}, 800);
"""


class RobustSSLAdapter(HTTPAdapter):
    """
    HTTPAdapter that configures TLS ciphers to prevent UNEXPECTED_EOF_WHILE_READING
    errors when connecting to CloudFront / AmazonS3 endpoints.
    """
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        ctx.check_hostname = False
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = RobustSSLAdapter(max_retries=retries)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Referer': 'https://fliphtml5.com/'
    })
    return session


def parse_book_code(user_input: str) -> str:
    """
    Extracts the '*****/****' book code from user input (URL or raw code).
    """
    raw = user_input.strip().strip('"').strip("'")
    if not raw:
        return ""

    match = re.search(r'fliphtml5\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)', raw)
    if match:
        return f"{match.group(1)}/{match.group(2)}"

    match = re.search(r'([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)', raw)
    if match:
        return f"{match.group(1)}/{match.group(2)}"

    return raw.strip('/')


def decrypt_pages_with_deno(encrypted_str: str) -> list:
    """
    Decrypts WASM-encrypted fliphtml5_pages string using Deno and FlipHTML5's deString.js.
    Uses stdin streaming to completely bypass Windows command-line character length limits.
    """
    deno_path = (
        shutil.which('deno')
        or (r"C:\Users\jerry\.deno\bin\deno.exe" if os.path.exists(r"C:\Users\jerry\.deno\bin\deno.exe") else None)
    )

    if not deno_path:
        raise RuntimeError(
            "该书籍页面经过 FlipHTML5 加密，需要 Deno 环境执行解密，但未检测到 Deno。\n"
            "请确保 Deno 已安装并加入环境变量。"
        )

    print("正在解密书籍页面配置 (WASM)...")

    # Locate or create decrypt.js
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    decrypt_js_path = os.path.join(base_dir, "decrypt.js")
    if not os.path.exists(decrypt_js_path):
        with open(decrypt_js_path, "w", encoding="utf-8") as f:
            f.write(DECRYPT_JS_CONTENT)

    proc = subprocess.run(
        [deno_path, "run", "-A", decrypt_js_path],
        input=encrypted_str,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )

    if proc.returncode != 0:
        raise RuntimeError(f"解密执行失败: {proc.stderr}")

    stdout_text = proc.stdout
    start = stdout_text.find('[')
    end = stdout_text.rfind(']')

    if start == -1 or end == -1 or start >= end:
        raise RuntimeError(f"未能从解密输出中提取到有效的页面数组: {stdout_text[:300]}")

    return json.loads(stdout_text[start:end + 1])


def download_single_page(session: requests.Session, book_name: str, page_idx: int, page_info: dict, folder_name: str):
    """
    Downloads a single page image trying standard URL patterns.
    """
    page_n = ""
    if isinstance(page_info, dict):
        n_list = page_info.get('n', [])
        if n_list and isinstance(n_list, list):
            page_n = str(n_list[0])
        elif 'n' in page_info:
            page_n = str(page_info['n'])

    # Candidate URLs
    candidates = []
    clean_n = page_n.lstrip('./').lstrip('/')
    if clean_n:
        if clean_n.startswith('files/'):
            candidates.append(f'https://online.fliphtml5.com/{book_name}/{clean_n}')
        else:
            candidates.append(f'https://online.fliphtml5.com/{book_name}/files/large/{clean_n}')
            candidates.append(f'https://online.fliphtml5.com/{book_name}/{clean_n}')

    candidates.append(f'https://online.fliphtml5.com/{book_name}/files/large/{page_idx + 1}.webp')
    candidates.append(f'https://online.fliphtml5.com/{book_name}/files/large/{page_idx + 1}.jpg')
    candidates.append(f'https://online.fliphtml5.com/{book_name}/files/page/{page_idx + 1}.jpg')

    downloaded_bytes = None
    for url in candidates:
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 500:
                downloaded_bytes = resp.content
                break
        except Exception:
            continue

    if downloaded_bytes:
        ext = "jpg"
        if downloaded_bytes.startswith(b'RIFF') and b'WEBP' in downloaded_bytes[:16]:
            ext = "webp"
        elif downloaded_bytes.startswith(b'\x89PNG'):
            ext = "png"

        file_path = os.path.join(folder_name, f"{page_idx + 1:04d}.{ext}")
        with open(file_path, "wb") as f:
            f.write(downloaded_bytes)
        return (page_idx, downloaded_bytes, None)
    else:
        return (page_idx, None, f"页面 {page_idx + 1} 下载失败 (未找到有效图片资源)")


def download_book(book_name: str):
    config_file_url = f'https://online.fliphtml5.com/{book_name}/javascript/config.js'
    print(f"\n正在获取书籍配置: {config_file_url} ...")

    session = create_session()

    try:
        config_resp = session.get(config_file_url, timeout=20)
    except Exception as e:
        print(f"【网络错误】请求书籍配置失败: {e}")
        return

    if config_resp.status_code != 200:
        print(f"【错误】无法获取书籍配置信息 (HTTP 状态码: {config_resp.status_code})。")
        print("请检查输入的链接或书籍编号是否正确！")
        return

    text = config_resp.text
    start_idx = text.find('{')
    end_idx = text.rfind('}')

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        print("【解析错误】未在服务器响应中找到有效的 JSON 配置信息。")
        return

    try:
        config_dict = json.loads(text[start_idx:end_idx + 1])
    except Exception as e:
        print(f"【JSON解析错误】配置文件解析失败: {e}")
        return

    # 书籍标题
    title = config_dict.get('meta', {}).get('title', 'fliphtml5_book')
    folder_name = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "fliphtml5_book"
    os.makedirs(folder_name, exist_ok=True)

    # 页面配置
    raw_pages = config_dict.get('fliphtml5_pages')
    if not raw_pages:
        print("【错误】书籍配置中未找到页面信息 (fliphtml5_pages 为空)。")
        return

    if isinstance(raw_pages, list):
        fliphtml5_pages = raw_pages
    elif isinstance(raw_pages, str):
        try:
            fliphtml5_pages = decrypt_pages_with_deno(raw_pages)
        except Exception as e:
            print(f"【错误】解密页面失败: {e}")
            return
    else:
        print("【错误】未知的 fliphtml5_pages 数据格式。")
        return

    total_pages = len(fliphtml5_pages)
    print(f"\n==========================================")
    print(f"书籍名称: 《{title}》")
    print(f"总页数: {total_pages} 页")
    print(f"保存目录: {folder_name}/")
    print(f"正在多线程极速下载中，请稍候...")
    print(f"==========================================\n")

    page_results = [None] * total_pages
    completed_count = 0

    # Multi-threaded downloading
    workers = min(8, max(2, total_pages))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                download_single_page,
                session,
                book_name,
                i,
                fliphtml5_pages[i] if i < len(fliphtml5_pages) else {},
                folder_name
            ): i for i in range(total_pages)
        }

        for future in as_completed(futures):
            page_idx, data, err = future.result()
            completed_count += 1
            if data:
                page_results[page_idx] = data
                print(f"\r进度: [{completed_count}/{total_pages}] (第 {page_idx + 1} 页下载成功)", end="", flush=True)
            else:
                print(f"\n警告: {err}")

    print("\n\n所有页面下载处理完成，正在优化并合成 PDF...")
    valid_images = [img for img in page_results if img is not None]

    if not valid_images:
        print("【错误】未成功下载到任何页面图片，无法生成 PDF。")
        return

    # Convert WebP/PNG to optimized JPEG (PDF does not natively support WebP, so raw WebP bloats PDF by 6x)
    def prepare_pdf_image(raw_bytes):
        if (raw_bytes.startswith(b'RIFF') and b'WEBP' in raw_bytes[:16]) or raw_bytes.startswith(b'\x89PNG'):
            try:
                img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82, optimize=True)
                return buf.getvalue()
            except Exception:
                pass
        return raw_bytes

    with ThreadPoolExecutor(max_workers=8) as ex:
        pdf_ready_images = list(ex.map(prepare_pdf_image, valid_images))

    pdf_path = f"{folder_name}.pdf"
    try:
        with open(pdf_path, "wb") as file:
            file.write(img2pdf.convert(pdf_ready_images))
        file_size_mb = round(os.path.getsize(pdf_path) / (1024 * 1024), 2)
        print(f"【成功】已生成优化版 PDF: {os.path.abspath(pdf_path)}")
        print(f"文件大小: {file_size_mb} MB，共包含 {len(pdf_ready_images)} / {total_pages} 页。")
    except Exception as e:
        print(f"【错误】生成 PDF 时失败: {e}")


def main():
    print("=" * 50)
    print("        FlipHTML5 Downloader (增强版)        ")
    print("=" * 50)

    while True:
        try:
            user_input = input("\n请输入书籍链接或代码 (格式形如 hgzok/boba 或完整URL，输入 q 退出):\n")
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

        if not user_input or user_input.strip().lower() in ('q', 'quit', 'exit'):
            break

        book_name = parse_book_code(user_input)
        if not book_name or '/' not in book_name:
            print("【格式错误】无法识别书籍编号，请输入类似 'hgzok/boba' 或包含该格式的完整 URL。")
            continue

        download_book(book_name)
        print("\n" + "-" * 50)


if __name__ == "__main__":
    main()
