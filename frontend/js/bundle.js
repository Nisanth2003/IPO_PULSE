/* One .zip per upload: the video, the narration, and the words.
 *
 * ── Why this is hand-written and not a library ────────────────────────────
 *
 * The two big members are a .webm and an .mp3, both already compressed. Deflate
 * on them buys somewhere between nothing and a rounding error, so this uses ZIP
 * method 0 — "stored" — and once you are not compressing, a ZIP file is just
 * headers around bytes. That is ~90 lines, most of it a CRC table.
 *
 * The alternative was a fourth CDN script tag next to Tailwind, html2canvas and
 * Alpine, loaded on every page view, to do a job the format does not require.
 *
 * ── The format, so the byte offsets below are readable ────────────────────
 *
 * For each file:  [local header][name][data]
 * Then:           [central directory entry per file]
 * Then:           [end-of-central-directory record]
 *
 * The central directory is what unzip programs actually read; the local
 * headers are a streaming convenience. Both carry the size and CRC, and they
 * MUST agree — a mismatch is the classic "archive is corrupt" report, so both
 * are written from the same computed values rather than twice from scratch.
 *
 * Everything is little-endian. ZIP is a DOS format and has never stopped being
 * one, which is also why the timestamp is packed into DOS date/time fields.
 */

const ZIP = (() => {
  'use strict';

  /* Standard CRC-32 (the one ZIP, PNG and gzip all use), table built once.
     The polynomial is reversed 0x04C11DB7 — the reversed form is what the
     byte-at-a-time loop below needs. */
  const TABLE = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[i] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) {
      c = TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    }
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  /* DOS timestamp: date is (year-1980)<<9 | month<<5 | day, time is
     hour<<11 | minute<<5 | (second/2). Two-second resolution, because 1980. */
  function dosStamp(when) {
    const date = ((when.getFullYear() - 1980) << 9)
               | ((when.getMonth() + 1) << 5)
               | when.getDate();
    const time = (when.getHours() << 11)
               | (when.getMinutes() << 5)
               | Math.floor(when.getSeconds() / 2);
    return { date: date & 0xFFFF, time: time & 0xFFFF };
  }

  /* A growable little-endian byte writer. Simpler to reason about than
     tracking offsets into a pre-sized DataView, and the archives here are
     megabytes, not gigabytes. */
  function Writer() {
    const parts = [];
    let length = 0;
    const push = (bytes) => { parts.push(bytes); length += bytes.length; };
    return {
      get length() { return length; },
      u16(n) { push(new Uint8Array([n & 0xFF, (n >>> 8) & 0xFF])); },
      u32(n) {
        push(new Uint8Array([n & 0xFF, (n >>> 8) & 0xFF,
                             (n >>> 16) & 0xFF, (n >>> 24) & 0xFF]));
      },
      raw(bytes) { push(bytes); },
      blob(type) { return new Blob(parts, { type }); },
    };
  }

  const utf8 = (s) => new TextEncoder().encode(s);

  /* files: [{ name, bytes: Uint8Array }]  ->  Blob
   *
   * `when` is a parameter rather than a `new Date()` inside, so a caller that
   * wants reproducible output can pass a fixed instant. */
  function build(files, when) {
    const stamp = dosStamp(when || new Date());
    const out = Writer();
    const index = [];

    for (const file of files) {
      const name = utf8(file.name);
      const data = file.bytes;
      const crc = crc32(data);
      index.push({ name, crc, size: data.length, offset: out.length });

      out.u32(0x04034B50);       // local file header signature
      out.u16(20);               // version needed: 2.0
      out.u16(0x0800);           // flags: bit 11 = the name is UTF-8
      out.u16(0);                // method 0 = stored
      out.u16(stamp.time);
      out.u16(stamp.date);
      out.u32(crc);
      out.u32(data.length);      // compressed size — same thing, stored
      out.u32(data.length);      // uncompressed size
      out.u16(name.length);
      out.u16(0);                // extra field length
      out.raw(name);
      out.raw(data);
    }

    const dirStart = out.length;
    for (const entry of index) {
      out.u32(0x02014B50);       // central directory signature
      out.u16(20);               // version made by
      out.u16(20);               // version needed
      out.u16(0x0800);
      out.u16(0);
      out.u16(stamp.time);
      out.u16(stamp.date);
      out.u32(entry.crc);
      out.u32(entry.size);
      out.u32(entry.size);
      out.u16(entry.name.length);
      out.u16(0);                // extra
      out.u16(0);                // comment
      out.u16(0);                // disk number
      out.u16(0);                // internal attributes
      out.u32(0);                // external attributes
      out.u32(entry.offset);     // where the local header is
      out.raw(entry.name);
    }
    const dirSize = out.length - dirStart;

    out.u32(0x06054B50);         // end of central directory
    out.u16(0);                  // this disk
    out.u16(0);                  // disk with the directory
    out.u16(index.length);       // entries on this disk
    out.u16(index.length);       // entries total
    out.u32(dirSize);
    out.u32(dirStart);
    out.u16(0);                  // comment length

    return out.blob('application/zip');
  }

  /* Convenience for the two shapes the studio actually has: text it generated
     and Blobs it captured. Both end up as Uint8Array so build() has one case. */
  async function fromMixed(entries, when) {
    const files = [];
    for (const e of entries) {
      if (!e || !e.name) continue;
      if (typeof e.text === 'string') {
        if (!e.text.trim()) continue;
        files.push({ name: e.name, bytes: utf8(e.text) });
      } else if (e.blob && e.blob.size) {
        files.push({ name: e.name,
                     bytes: new Uint8Array(await e.blob.arrayBuffer()) });
      }
    }
    return files.length ? build(files, when) : null;
  }

  return { build, fromMixed, crc32 };
})();
