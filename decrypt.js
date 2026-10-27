// decrypt.js: Decrypts fliphtml5_pages string passed via stdin
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

// Read encrypted string from stdin
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

// Wait for runtime to be ready then decrypt
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
