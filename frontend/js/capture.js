/* Record the reel and the narration into one file.
 *
 * ── What this replaces ────────────────────────────────────────────────────
 *
 * The old daily loop was: press F, press Space, screen-record the region with
 * an external recorder, generate the voice separately, then lay the voice in
 * CapCut and cut the video to it. Steps 4, 6 and part of 7 of the playbook.
 *
 * This does the recording and the mux in one pass: the browser captures the
 * tab, the mp3 is fed in as a second track, and MediaRecorder writes a single
 * .webm with the narration already in it. Which finally makes the Script
 * timing mode pay: the scene holds and the narration come from the same text,
 * so they line up without a sync pass.
 *
 * ── Why getDisplayMedia and not canvas.captureStream ──────────────────────
 *
 * The reel is live DOM — Tailwind, gradients, web fonts, an animated GIF
 * sticker — not a canvas. There is nothing to call captureStream() on. The
 * alternative is html2canvas per frame, which takes ~100-400ms for one card
 * and cannot produce 30fps by three orders of magnitude. So the browser's own
 * screen capture is the only route that renders what you actually see.
 *
 * Two consequences that cannot be engineered away, only planned around:
 *
 *   1. It needs a user gesture and a picker click EVERY time. There is no
 *      permission to remember and no way to pre-select the tab. One click per
 *      recording, forever.
 *   2. It captures the tab, panels and all. Focus mode (F) stops being a nice
 *      touch and becomes mandatory — so startCapture takes care of it rather
 *      than trusting anyone to remember at 6am.
 *
 * ── On the audio ──────────────────────────────────────────────────────────
 *
 * The mp3 is decoded through WebAudio and re-encoded as Opus inside the webm,
 * which is a real generation loss. It is inaudible for spoken narration at
 * 128kbps in and ~128kbps out, and the alternative — muxing the mp3 through
 * untouched — is not something MediaRecorder can do. The bundle ships the
 * pristine voice.mp3 alongside the video anyway, so a re-edit never has to
 * use the transcoded copy.
 */

const CAPTURE = (() => {
  'use strict';

  const supported = !!(navigator.mediaDevices
                    && navigator.mediaDevices.getDisplayMedia
                    && window.MediaRecorder);

  /* First supported container wins. VP9 is smaller at the same quality and
     every current browser that has MediaRecorder has it; VP8 is the fallback
     for older Safari/Firefox builds. YouTube accepts webm directly, so there
     is no transcode step waiting after this. */
  function pickMime() {
    const options = [
      'video/webm;codecs=vp9,opus',
      'video/webm;codecs=vp8,opus',
      'video/webm',
    ];
    for (const m of options) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return '';
  }

  /* An <audio> element routed into a MediaStream, so MediaRecorder gets a
     real audio track it can encode.

     An <audio> element on its own is NOT capturable — playing it into the page
     puts sound on the speakers, not into a stream, and the recording comes out
     silent. createMediaElementSource is what redirects it, and connecting to
     BOTH the stream destination and the real one is what keeps it audible
     while recording: once an element is routed through WebAudio, the default
     path to the speakers is gone. */
  function audioTrack(url, ctx) {
    const el = new Audio(url);
    el.crossOrigin = 'anonymous';
    const source = ctx.createMediaElementSource(el);
    const sink = ctx.createMediaStreamDestination();
    source.connect(sink);
    source.connect(ctx.destination);
    return { el, track: sink.stream.getAudioTracks()[0] };
  }

  /* opts: { voiceUrl, onState(text), onStop(blob), play(), reset() }
   *
   * `play` and `reset` are handed in rather than reached for, so this file
   * knows nothing about the studio's Alpine component and can be read on its
   * own. */
  async function start(opts) {
    if (!supported) throw new Error(
      'This browser cannot record: it has no getDisplayMedia or MediaRecorder. '
      + 'Chrome or Edge on the desktop can.');

    const mime = pickMime();
    if (!mime) throw new Error('No webm encoder in this browser.');

    const say = opts.onState || (() => {});

    /* Ask for the screen FIRST. It must be the first thing after the click or
       the browser drops the user-gesture flag and refuses — an await on
       anything slower than a microtask in between is enough to break it. */
    say('Pick this tab in the dialog…');
    const display = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 30 },
      audio: false,          // page audio would double the narration
    });

    const video = display.getVideoTracks()[0];
    const mixed = new MediaStream([video]);

    let ctx = null;
    let voice = null;
    if (opts.voiceUrl) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      voice = audioTrack(opts.voiceUrl, ctx);
      mixed.addTrack(voice.track);
    }

    const chunks = [];
    const recorder = new MediaRecorder(mixed, {
      mimeType: mime,
      videoBitsPerSecond: 6_000_000,   // plenty for flat cards and big type
      audioBitsPerSecond: 128_000,
    });
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };

    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      display.getTracks().forEach((t) => t.stop());
      if (voice) { voice.el.pause(); }
      if (ctx) { ctx.close().catch(() => {}); }
      const blob = new Blob(chunks, { type: mime });
      say('');
      if (opts.onStop) opts.onStop(blob);
    };
    recorder.onstop = finish;

    /* The user can end the capture from the browser's own "Stop sharing" bar,
       which kills the track without telling the recorder. Without this the
       recording would run on against a dead source and never produce a file. */
    video.addEventListener('ended', () => {
      if (recorder.state !== 'inactive') recorder.stop();
    });

    say('Recording…');
    recorder.start(250);          // timeslice, so a crash still leaves chunks

    /* Rewind to scene 1 before rolling. The picker takes a few seconds and
       leaves the reel wherever it was; a recording that opens mid-reel is the
       most likely way to waste a take. */
    if (opts.reset) opts.reset();
    if (voice) {
      try { await ctx.resume(); } catch (e) { /* already running */ }
      voice.el.currentTime = 0;
      await voice.el.play().catch(() => {});
    }
    if (opts.play) opts.play();

    return {
      mime,
      stop() { if (recorder.state !== 'inactive') recorder.stop(); else finish(); },
      get state() { return recorder.state; },
    };
  }

  return { supported, start, pickMime };
})();
