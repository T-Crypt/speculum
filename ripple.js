/* =========================================================================
   ripple.js — the single, isolated ripple implementation for the app.
   One 400ms radial pulse (max opacity 0.08, accent color) on data update,
   never on hover. Off by default; persisted under "speculum.ui.ripple";
   forced off whenever prefers-reduced-motion: reduce is set, whatever the
   setting says. Enabled via <html data-ripple="on">; while off the module
   holds no listeners and pulse() is a no-op (zero cost).
   ========================================================================= */
import { prefs, motionReduced } from './ui.js';

const PERIOD_MS = 416; // 400ms animation + one frame of margin

/* Apply the persisted + OS setting to the root attribute. Returns true
   while the effect is active. Call on boot and after any setting change. */
export function applyRippleSetting() {
  const active = !motionReduced() && prefs.ripple === 'on';
  if (active) document.documentElement.setAttribute('data-ripple', 'on');
  else document.documentElement.removeAttribute('data-ripple');
  return active;
}

const timers = new WeakMap();

/* Trigger one pulse on a panel element (a .panel root). The paint layer
   calls this once per data update for panels whose data changed. */
export function pulse(panel) {
  if (panel == null) return;
  if (document.documentElement.getAttribute('data-ripple') !== 'on') return;
  if (motionReduced()) { applyRippleSetting(); return; } // setting drifted under an OS override
  if (timers.has(panel)) clearTimeout(timers.get(panel));
  panel.classList.remove('is-rippling');
  void panel.offsetWidth; // restart the animation on back-to-back updates
  panel.classList.add('is-rippling');
  timers.set(panel, setTimeout(() => panel.classList.remove('is-rippling'), PERIOD_MS));
}

/* Pulse several panels at once (e.g. a tick that touches many). */
export function pulseAll(panels) {
  for (const p of panels) pulse(p);
}
