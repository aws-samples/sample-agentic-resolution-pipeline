'use strict';

/**
 * Tests for slidingWindow.js
 *
 * Regression suite for IOT-6: slice(1) off-by-one bug that caused getDeviceWindow()
 * to return N-1 samples instead of N, leading to missed and false/flapping alerts.
 */

const { getDeviceWindow, clearDeviceWindow, getWindowConfig, WINDOW_SIZE } = require('../src/slidingWindow');

// Helper: push `count` samples into the live window buffer by reading and mutating it
async function addSamples(deviceId, metric, count, baseValue = 80) {
  for (let i = 0; i < count; i++) {
    const buf = await getDeviceWindow(deviceId, metric);
    buf.push({ value: baseValue + i, timestamp: new Date().toISOString() });
  }
}

describe('Sliding Window — IOT-6 regression suite', () => {
  const DEVICE = 'test-device';
  const METRIC = 'temperature_c';

  beforeEach(async () => {
    await clearDeviceWindow(DEVICE, METRIC);
    await clearDeviceWindow('single-device', 'speed_kmh');
    await clearDeviceWindow('warming-device', METRIC);
    await clearDeviceWindow('overflow-device', METRIC);
    await clearDeviceWindow('trkdevice', METRIC);
  });

  // ── 1. Baseline ─────────────────────────────────────────────────────────────

  test('returns empty array for a brand-new device', async () => {
    const result = await getDeviceWindow('brand-new-device', METRIC);
    expect(result).toEqual([]);
  });

  // ── 2. Full window — core IOT-6 regression ──────────────────────────────────

  test('full window (WINDOW_SIZE samples) returns all N samples — not N-1', async () => {
    await addSamples(DEVICE, METRIC, WINDOW_SIZE);
    const result = await getDeviceWindow(DEVICE, METRIC);
    // REGRESSION: slice(1) would return WINDOW_SIZE - 1 here
    expect(result.length).toBe(WINDOW_SIZE);
  });

  test('5-sample sub-full buffer returns exactly 5 samples', async () => {
    await addSamples(DEVICE, METRIC, 5);
    const result = await getDeviceWindow(DEVICE, METRIC);
    // REGRESSION: slice(1) would return 4 here
    expect(result.length).toBe(5);
  });

  // ── 3. window_size=1 edge case ───────────────────────────────────────────────

  test('window_size=1 — single sample is included, not silently dropped', async () => {
    const buf = await getDeviceWindow('single-device', 'speed_kmh');
    buf.push({ value: 100, timestamp: new Date().toISOString() });

    const result = await getDeviceWindow('single-device', 'speed_kmh');
    // REGRESSION: slice(1) returned 0 samples — the one sample was thrown away
    expect(result.length).toBe(1);
    expect(result[0].value).toBe(100);
  });

  // ── 4. Missed alert regression ───────────────────────────────────────────────

  test('missed-alert: 10th sample pushes average over threshold — alert must fire', async () => {
    // Temperature threshold is 85 °C (from alertEvaluator ALERT_RULES).
    // With 9 samples at 84 and 1 sample at 95 the 10-sample average is
    //   (84*9 + 95) / 10 = 851/10 = 85.1  → above threshold → alert fires.
    // With 9 samples (slice(1) bug), the oldest 84 is dropped:
    //   (84*8 + 95) / 9 = 767/9 ≈ 85.2 — still fires but for the wrong reason.
    // The real regression scenario: all 10 samples are exactly at the boundary.
    //   9 samples = 84.89 avg (below 85) but 10 samples = 85 avg (at/above threshold).
    const THRESHOLD = 85;
    // 9 × 84 + 1 × 94 = 756 + 94 = 850; 850/10 = 85.0 — exactly at threshold
    const values = [...Array(9).fill(84), 94];

    const buf = await getDeviceWindow(DEVICE, METRIC);
    values.forEach(v => buf.push({ value: v, timestamp: new Date().toISOString() }));

    const result = await getDeviceWindow(DEVICE, METRIC);
    const avg = result.reduce((s, s_) => s + s_.value, 0) / result.length;

    // Must have all 10 samples to compute the correct average
    expect(result.length).toBe(10);
    expect(avg).toBeCloseTo(85.0, 5);
    expect(avg).toBeGreaterThanOrEqual(THRESHOLD);
  });

  // ── 5. No false flap regression ──────────────────────────────────────────────

  test('no-false-flap: warming device — alert state does not toggle between consecutive evaluations', async () => {
    const THRESHOLD = 85;

    // Simulate a warming sequence: all readings below threshold
    const warmingValues = [70, 72, 74, 75, 76, 77, 78, 79, 80, 81];

    const buf = await getDeviceWindow('warming-device', METRIC);
    warmingValues.forEach(v => buf.push({ value: v, timestamp: new Date().toISOString() }));

    // Evaluate two consecutive cycles (no new data arrives)
    const w1 = await getDeviceWindow('warming-device', METRIC);
    const avg1 = w1.reduce((s, s_) => s + s_.value, 0) / w1.length;
    const alerted1 = avg1 > THRESHOLD;

    const w2 = await getDeviceWindow('warming-device', METRIC);
    const avg2 = w2.reduce((s, s_) => s + s_.value, 0) / w2.length;
    const alerted2 = avg2 > THRESHOLD;

    // Both evaluations should agree — no flapping
    expect(alerted1).toBe(false);
    expect(alerted2).toBe(false);
    expect(alerted1).toBe(alerted2);
  });

  // ── 6. Buffer overflow — sliding correctly evicts oldest ─────────────────────

  test('buffer overflow: adding WINDOW_SIZE+5 samples caps at WINDOW_SIZE and keeps the newest', async () => {
    const extraCount = WINDOW_SIZE + 5;
    await addSamples('overflow-device', METRIC, extraCount, 10);

    const result = await getDeviceWindow('overflow-device', METRIC);
    expect(result.length).toBe(WINDOW_SIZE);

    // The retained values should be the last WINDOW_SIZE ones (10+5 through 10+extraCount-1)
    const lastValue = result[result.length - 1].value;
    expect(lastValue).toBe(10 + extraCount - 1);
  });

  // ── 7. Returned array is a copy, not a live reference ────────────────────────

  test('mutating the returned array does not corrupt the internal buffer', async () => {
    await addSamples(DEVICE, METRIC, 3);

    const copy = await getDeviceWindow(DEVICE, METRIC);
    const originalLength = copy.length;
    copy.push({ value: 999, timestamp: 'mutated' }); // mutate the copy

    const fresh = await getDeviceWindow(DEVICE, METRIC);
    // Internal buffer must be unchanged
    expect(fresh.length).toBe(originalLength);
    expect(fresh.some(s => s.value === 999)).toBe(false);
  });

  // ── 8. clearDeviceWindow resets state ────────────────────────────────────────

  test('clearDeviceWindow resets the window to empty', async () => {
    await addSamples(DEVICE, METRIC, 5);
    await clearDeviceWindow(DEVICE, METRIC);

    const result = await getDeviceWindow(DEVICE, METRIC);
    expect(result).toEqual([]);
  });

  // ── 9. getWindowConfig returns correct constants ──────────────────────────────

  test('getWindowConfig returns windowSize matching WINDOW_SIZE export', () => {
    const config = getWindowConfig();
    expect(config.windowSize).toBe(WINDOW_SIZE);
    expect(typeof config.ttlSeconds).toBe('number');
    expect(config.ttlSeconds).toBeGreaterThan(0);
  });

  // ── 10. TRK-005 scenario: window_count in alert payload reports 10, not 9 ────

  test('TRK-005 regression: window_count in evaluator result is WINDOW_SIZE, not WINDOW_SIZE-1', async () => {
    // Reproduces the TRK-005 "windowSize=9" field reported in alert payloads.
    const { evaluateAlert } = require('../src/alertEvaluator');

    const samples = Array.from({ length: WINDOW_SIZE }, (_, i) => ({
      value: 86 + i * 0.1, // all above threshold of 85
      timestamp: new Date().toISOString(),
    }));

    const buf = await getDeviceWindow('trkdevice', METRIC);
    samples.forEach(s => buf.push(s));

    const window = await getDeviceWindow('trkdevice', METRIC);
    const result = evaluateAlert('trkdevice', METRIC, samples[samples.length - 1].value, window);

    expect(result.triggered).toBe(true);
    // REGRESSION: slice(1) would report window_count = WINDOW_SIZE - 1
    expect(result.window_count).toBe(WINDOW_SIZE);
  });
});
