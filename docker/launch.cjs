'use strict';
// stdin is the only transport for credentials and prompt. Never log either.
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

function exactKeys(obj, allowed) {
  return obj !== null && typeof obj === 'object' && !Array.isArray(obj)
    && Object.keys(obj).every(key => allowed.includes(key));
}
function validate(envelope) {
  if (!exactKeys(envelope, ['auth', 'prompt']) || typeof envelope.prompt !== 'string'
      || !envelope.prompt.trim() || !exactKeys(envelope.auth, ['auth_mode', 'tokens'])
      || envelope.auth.auth_mode !== 'chatgpt'
      || !exactKeys(envelope.auth.tokens, ['access_token', 'id_token', 'refresh_token', 'account_id'])) {
    throw new Error('invalid transport');
  }
  const tokens = envelope.auth.tokens;
  for (const key of ['access_token', 'id_token', 'refresh_token']) {
    if (typeof tokens[key] !== 'string' || !tokens[key].trim()) throw new Error('invalid credentials');
  }
  if ('account_id' in tokens && (typeof tokens.account_id !== 'string' || !tokens.account_id.trim())) {
    throw new Error('invalid account');
  }
  return envelope;
}
async function main(argv) {
  let authPath;
  let written = false;
  try {
    if (!argv.length) throw new Error('missing command');
    const chunks = [];
    let bytes = 0;
    for await (const chunk of process.stdin) {
      bytes += chunk.length;
      if (bytes > 4 * 1024 * 1024) throw new Error('transport too large');
      chunks.push(chunk);
    }
    const envelope = validate(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    const home = process.env.CODEX_HOME || '/home/codex/.codex';
    fs.mkdirSync(home, {recursive: true, mode: 0o700});
    authPath = path.join(home, 'auth.json');
    fs.writeFileSync(authPath, JSON.stringify(envelope.auth), {mode: 0o600, flag: 'wx'});
    written = true;
    const code = await new Promise((resolve, reject) => {
      const child = spawn(argv[0], argv.slice(1), {shell: false, stdio: ['pipe', 'pipe', 'ignore']});
      child.once('error', reject);
      child.stdout.pipe(process.stdout);
      child.stdin.on('error', () => {});
      child.stdin.end(envelope.prompt);
      child.once('close', (status) => resolve(status === null ? 1 : status));
    });
    if (code !== 0) process.stderr.write('Codex execution failed.\n');
    return code;
  } catch {
    process.stderr.write('Codex bootstrap failed.\n');
    return 1;
  } finally {
    if (written) {
      try { fs.unlinkSync(authPath); }
      catch { /* Container tmpfs is removed by docker --rm as the final boundary. */ }
    }
  }
}
module.exports = {validate, main};
if (require.main === module) main(process.argv.slice(2)).then(code => { process.exitCode = code; });
