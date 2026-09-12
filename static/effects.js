"use strict";

// Chess Amateur: client-side effects — synthesized sound (Web Audio API, no
// files) + settings (animations / sound) with persistence and the dependency
// rule that sound requires animations.
//
// Settings rule (per product spec):
//   * Two toggles: animations (piece sliding) and sound.
//   * Sound may be ON only when animations are ON. The single forbidden combo
//     is animations-OFF + sound-ON, because with no sliding animation the
//     player's move and Chess Amateur's reply resolve at the same instant, so
//     their two "knock" sounds would play simultaneously. With animations on,
//     the slides stagger the two sounds.
//   * So: turning animations OFF forces sound OFF (and the sound toggle is
//     disabled in the UI); turning animations ON re-enables the sound toggle.
//
// Persistence: both booleans are stored in localStorage (tiny), applying to
// guests and logged-in users alike.

const LS_ANIM = "ca_animations";
const LS_SOUND = "ca_sound";

const Settings = (() => {
  function _load(key, dflt) {
    try {
      const v = window.localStorage.getItem(key);
      if (v === null) return dflt;
      return v === "1";
    } catch (e) { return dflt; }
  }
  function _save(key, val) {
    try { window.localStorage.setItem(key, val ? "1" : "0"); } catch (e) {}
  }

  // Defaults: animations on, sound on.
  let animations = _load(LS_ANIM, true);
  let sound = _load(LS_SOUND, true);
  // Enforce the invariant on load (sound implies animations).
  if (!animations) sound = false;

  return {
    get animations() { return animations; },
    get sound() { return sound; },
    setAnimations(on) {
      animations = !!on;
      _save(LS_ANIM, animations);
      // Sound cannot be on without animations.
      if (!animations && sound) { sound = false; _save(LS_SOUND, sound); }
      return { animations, sound };
    },
    setSound(on) {
      // Ignore attempts to enable sound while animations are off.
      sound = animations ? !!on : false;
      _save(LS_SOUND, sound);
      return { animations, sound };
    },
  };
})();


// --- Synthesized sound via Web Audio API ---------------------------------
const Sound = (() => {
  let ctx = null;
  function _ctx() {
    if (ctx === null) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    // Browsers start the context suspended until a user gesture; resume it.
    if (ctx.state === "suspended") { try { ctx.resume(); } catch (e) {} }
    return ctx;
  }

  // A short percussive "knock": a fast-decaying sine blip. `freq` sets pitch
  // (higher for a check knock).
  function knock(freq) {
    if (!Settings.sound) return;
    const ac = _ctx();
    if (!ac) return;
    const t0 = ac.currentTime;
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(freq, t0);
    // Quick pitch drop gives it a "tock" character.
    osc.frequency.exponentialRampToValueAtTime(Math.max(60, freq * 0.5), t0 + 0.06);
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(0.35, t0 + 0.005);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.12);
    osc.connect(gain).connect(ac.destination);
    osc.start(t0);
    osc.stop(t0 + 0.14);
  }

  // Play a short sequence of notes (each {f: freq, d: durationSec}). Used for
  // the ultra-short win/draw/loss tunes.
  function tune(notes) {
    if (!Settings.sound) return;
    const ac = _ctx();
    if (!ac) return;
    let t = ac.currentTime;
    for (const n of notes) {
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.type = "triangle";
      osc.frequency.setValueAtTime(n.f, t);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.3, t + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + n.d);
      osc.connect(gain).connect(ac.destination);
      osc.start(t);
      osc.stop(t + n.d + 0.02);
      t += n.d;
    }
  }

  return {
    move() { knock(200); },              // normal move: low knock
    check() { knock(520); },             // check: high-pitched knock
    win() { tune([{f:523,d:0.12},{f:659,d:0.12},{f:784,d:0.16}]); },   // happy, rising C-E-G
    draw() { tune([{f:440,d:0.14},{f:440,d:0.16}]); },                 // neutral, flat A-A
    loss() { tune([{f:392,d:0.14},{f:311,d:0.20}]); },                 // sad, falling G-Eb
  };
})();

// Expose for app.js.
window.CA_Settings = Settings;
window.CA_Sound = Sound;
