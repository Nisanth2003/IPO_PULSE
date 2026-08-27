/* The front door.
 *
 * ── What this actually protects, stated plainly ──────────────────────────
 *
 * This is a curtain, not a lock, and the difference matters enough to write
 * down before anyone relies on it.
 *
 * It DOES stop: someone who finds the URL, a search engine (see the noindex
 * tag in index.html), a colleague glancing at a shared screen, and anyone
 * who is not deliberately trying to get past it.
 *
 * It does NOT stop anyone who opens DevTools. This is a static site — there
 * is no server to check anything — so the check runs on the visitor's own
 * machine and the visitor owns that machine. The unlock is one line in the
 * console away.
 *
 * And the part that no password on this page can ever fix: **the Google
 * Sheet behind the studio is world-readable by necessity.** A keyless static
 * page can only read a sheet shared as "anyone with the link", the sheet id
 * ships in config.js, and both are public. Anyone who has the id can pull
 * every tab as CSV straight from Google without visiting this site at all.
 * So this gate hides the *studio*, not the *data*.
 *
 * If the data itself needs to be private, the fix is architectural, not a
 * password: serve the frontend from the backend, proxy the sheet reads
 * through it with the service account, and close the sheet. See
 * docs/YOUTUBE-PLAYBOOK.md's sibling note in the README.
 *
 * ── How it works ─────────────────────────────────────────────────────────
 *
 * config.js carries PBKDF2-SHA256(password, salt=sheet id, 310k iterations).
 * The password is never written anywhere the browser can read. A guess is
 * hashed the same way and compared; a match stores a flag for the session.
 * 310k iterations is what makes the shipped hash expensive to attack
 * offline — which is the whole reason it is a derived hash and not a plain
 * SHA-256 of the password.
 *
 * ── The second job: unsealing the Actions token ───────────────────────────
 *
 * config.js may also carry GH_PAT_CIPHER — the GitHub PAT that dispatches
 * schedule.yml, AES-GCM encrypted at build time under this same password
 * (backend/ipopulse/cli.py, _sealed_pat). The studio's Run job panel needs the
 * token itself, so it cannot work from a hash; a correct password is the only
 * thing that can produce one.
 *
 * Which is why the unseal lives HERE and not in studio.js: this is the only
 * file the password ever passes through, and it should stay that way.
 *
 * Note the salts differ on purpose. The gate hash is salted with the sheet id
 * and is PUBLIC; the token key is salted with GH_PAT_SALT, random per build.
 * Reusing one salt for both would publish the decryption key next to the
 * ciphertext it opens.
 *
 * The unsealed token is handed to the page in memory (window.GH_PAT) and
 * written to no storage at all — not localStorage, not sessionStorage. So a
 * reload inside an already-unlocked session has no token, and the Run job
 * panel asks for the password again at that point rather than keeping a
 * credential lying around for the tab's lifetime.
 */

(function () {
  'use strict';

  var KEY = 'ipoPulse.gate';
  var hash = (typeof SITE_GATE_HASH !== 'undefined' && SITE_GATE_HASH) || '';
  var iterations = (typeof SITE_GATE_ITER !== 'undefined' && SITE_GATE_ITER) || 0;
  var salt = (typeof SHEET_ID !== 'undefined' && SHEET_ID) || '';

  var host = location.hostname;
  var isLocal = host === 'localhost' || host === '127.0.0.1' ||
                host === '' || host === '[::1]';

  function open() { document.documentElement.classList.add('gate-open'); }

  /* ── the sealed Actions token ─────────────────────────────────────────── */

  var patCipher = (typeof GH_PAT_CIPHER !== 'undefined' && GH_PAT_CIPHER) || '';
  var patSalt = (typeof GH_PAT_SALT !== 'undefined' && GH_PAT_SALT) || '';
  var patIv = (typeof GH_PAT_IV !== 'undefined' && GH_PAT_IV) || '';

  function fromB64(b64) {
    var bin = atob(b64);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  /* password -> the PAT, or a rejection. Deliberately returns nothing at all
     rather than a partial result: AES-GCM authenticates, so a wrong password
     fails to decrypt instead of yielding plausible garbage that would reach
     api.github.com as a bad credential. */
  async function unsealPat(password) {
    if (!patCipher || !patSalt || !patIv) return '';
    var enc = new TextEncoder();
    var base = await crypto.subtle.importKey(
      'raw', enc.encode(password), { name: 'PBKDF2' }, false, ['deriveKey']);
    var key = await crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt: fromB64(patSalt),
        iterations: iterations, hash: 'SHA-256' },
      base, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
    var plain = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: fromB64(patIv) }, key, fromB64(patCipher));
    return new TextDecoder().decode(plain);
  }

  /* What the studio talks to. `sealed` lets the Run job panel decide between
     "ask for the site password" and "ask for a token to paste" without
     knowing anything about how the seal works. */
  window.IPO_GATE = {
    sealed: !!(patCipher && patSalt && patIv),
    unseal: unsealPat,
  };

  /* Called on every successful password entry. Failure is non-fatal and
     silent-ish: the seal is optional, an older config.js has no cipher, and a
     studio that refused to open because a token could not be unwrapped would
     be trading the whole tool for one convenience button. */
  async function adoptPat(password) {
    if (!window.IPO_GATE.sealed) return;
    try {
      window.GH_PAT = await unsealPat(password);
    } catch (err) {
      window.GH_PAT = '';
      window.IPO_GATE.error = 'The stored Actions token could not be '
        + 'unsealed with this password — it was probably sealed under a '
        + 'different one. Redeploy, or paste a token by hand.';
    }
  }

  /* No password configured.
   *
   * Locally that means "you have not set one", and blocking your own dev
   * server over it would be obnoxious. Anywhere else it means a secret is
   * missing from the deployment — and a gate that fails OPEN on a missing
   * secret is worse than no gate, because it looks protected and is not.
   * So: open on localhost, refuse everywhere else. */
  if (!hash || !iterations || !salt) {
    if (isLocal) { open(); return; }
    document.addEventListener('DOMContentLoaded', function () {
      document.body.innerHTML =
        '<div style="font:15px/1.6 system-ui;max-width:34rem;margin:18vh auto;' +
        'padding:0 1.5rem;color:#e5e7eb">' +
        '<h1 style="font-size:1.2rem;margin:0 0 .75rem">Site not configured</h1>' +
        '<p style="color:#9ca3af">No access password was built into this ' +
        'deployment, so it is refusing to open rather than publishing the ' +
        'studio to everyone.</p>' +
        '<p style="color:#9ca3af">Set <code>IPOPULSE_TRIGGER_PASSWORD</code> ' +
        'as a secret in the publish workflow and redeploy.</p></div>';
      document.documentElement.classList.add('gate-open');
    });
    return;
  }

  if (sessionStorage.getItem(KEY) === hash) { open(); return; }

  function toHex(buffer) {
    return Array.prototype.map
      .call(new Uint8Array(buffer), function (b) {
        return ('00' + b.toString(16)).slice(-2);
      }).join('');
  }

  async function derive(password) {
    var enc = new TextEncoder();
    var key = await crypto.subtle.importKey(
      'raw', enc.encode(password), { name: 'PBKDF2' }, false, ['deriveBits']);
    var bits = await crypto.subtle.deriveBits(
      { name: 'PBKDF2', salt: enc.encode(salt),
        iterations: iterations, hash: 'SHA-256' }, key, 256);
    return toHex(bits);
  }

  /* crypto.subtle exists only in a secure context — https, or localhost.
   * A site opened as file:// or served over plain http from another machine
   * has no WebCrypto at all, and silently opening there would be the same
   * fail-open bug as above. */
  var haveCrypto = !!(window.crypto && crypto.subtle);

  document.addEventListener('DOMContentLoaded', function () {
    var wrap = document.createElement('div');
    wrap.setAttribute('data-html2canvas-ignore', '');
    /* `visibility:visible` is load-bearing, not decoration. index.html hides
     * the whole body until `.gate-open`, and this overlay is a child of that
     * body — so without an explicit override it inherits the hiding and the
     * page renders as an empty dark rectangle with the form present but
     * invisible. visibility, unlike display, is inherited and can be turned
     * back on by a descendant, which is exactly why the page uses it. */
    wrap.style.cssText =
      'visibility:visible;position:fixed;inset:0;z-index:99999;display:flex;' +
      'align-items:center;justify-content:center;background:#0a0f1a;' +
      'font:15px/1.6 system-ui,-apple-system,Segoe UI,sans-serif';
    wrap.innerHTML =
      '<form style="width:min(22rem,88vw);text-align:left">' +
      '<div style="font-weight:800;font-size:1.35rem;color:#22C55E;' +
      'letter-spacing:-.02em">IPO Pulse</div>' +
      '<div style="color:#9ca3af;margin:.35rem 0 1.25rem">' +
      (haveCrypto ? 'Private studio. Enter the password to continue.'
                  : 'This page needs https (or localhost) to check the password.') +
      '</div>' +
      '<input type="password" autocomplete="current-password" autofocus ' +
      (haveCrypto ? '' : 'disabled ') +
      'style="width:100%;padding:.7rem .85rem;border-radius:.6rem;' +
      'border:1px solid #253048;background:#111827;color:#e5e7eb;' +
      'font-size:1rem;outline:none">' +
      '<button type="submit"' + (haveCrypto ? '' : ' disabled') +
      ' style="width:100%;margin-top:.6rem;padding:.7rem;border:0;' +
      'border-radius:.6rem;background:#22C55E;color:#04120a;font-weight:700;' +
      'font-size:.95rem;cursor:pointer">Unlock</button>' +
      '<div data-msg style="min-height:1.3rem;margin-top:.6rem;' +
      'color:#EF4444;font-size:.85rem"></div>' +
      '</form>';

    document.body.appendChild(wrap);
    var form = wrap.querySelector('form');
    var input = wrap.querySelector('input');
    var button = wrap.querySelector('button');
    var msg = wrap.querySelector('[data-msg]');

    /* Slows a human at a keyboard far more than it slows a script, but the
     * script is not the threat this can address anyway — see the header. */
    var tries = 0;

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!haveCrypto || !input.value) return;
      button.disabled = true;
      button.textContent = 'Checking…';
      msg.textContent = '';
      try {
        var got = await derive(input.value);
        if (got === hash) {
          sessionStorage.setItem(KEY, hash);
          /* Before the password goes out of scope — this is the only moment
             it exists in the page, and the token cannot be recovered later
             without it. Awaited so the studio never reads window.GH_PAT
             during the gap. */
          await adoptPat(input.value);
          wrap.remove();
          open();
          return;
        }
        tries++;
        msg.textContent = 'Not that one.' + (tries >= 3 ? ' Check your .env.' : '');
        input.select();
      } catch (err) {
        msg.textContent = 'Could not check the password: ' + err.message;
      }
      button.disabled = false;
      button.textContent = 'Unlock';
      /* A short, growing pause after each miss. */
      await new Promise(function (r) { setTimeout(r, Math.min(tries, 5) * 400); });
    });
  });
})();
