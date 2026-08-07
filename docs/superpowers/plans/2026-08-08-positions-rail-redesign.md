# Positions Rail Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the Positions-tab price rail so the real 52-week low and high are always shown correctly, CMP gets a visible price label, the Add level reads as a named level, and all five labels sit in their own row so none can ever collide.

**Architecture:** Backend change to `rail()` in `backend/calculator.py` separates the rail's scale bounds (which can still be pulled/stretched for layout reasons) from the five displayed price points (which must always show the real price). Frontend change to `renderPositions()` in `index.html` renders those five points as a 4-band stacked layout instead of the current 2-band absolute-position hack.

**Tech Stack:** Python (FastAPI backend, pytest), vanilla JS + CSS (single-file frontend, no build step).

## Global Constraints

- No new dependencies, no new files — this is a scoped change to two existing files (`backend/calculator.py`, `index.html`) plus their existing test file (`backend/tests/test_app.py`).
- No change to how TP, Add level, or 52W low/high are computed upstream (holdings file, signals feed) — display and scale-math only, per the spec's Non-goals.
- Frontend has no JS test framework — frontend verification is done via the local dev-server preview (Claude Browser tools), matching the pattern already used twice this session for other UI fixes: a DOM overflow scan (`scrollWidth > clientWidth`) across all six tabs at mobile (375px) and desktop widths, plus reading rendered text/values back to confirm correctness.
- Every commit in this session has been pushed to both `origin main` (`jprasham/dl-india-portfolio-dashboard`) and `core main:master` (`pangoriyaanigrah-maker/dl-india-core`) — keep doing that.

---

### Task 1: Backend — separate rail scale bounds from displayed price points

**Files:**
- Modify: `backend/calculator.py:468-483` (the `rail()` function)
- Test: `backend/tests/test_app.py` (append near the other direct `calculator.*` unit tests, e.g. after `test_give1m_survives_a_stock_that_went_to_zero` around line 505)

**Interfaces:**
- Consumes: nothing new — same inputs as today, `rail(h, s)` where `h` is a holding dict (`tp`, `addLvl`) and `s` is that ticker's signals dict (`cmp`, `lo`, `hi`).
- Produces: `rail()` return dict, consumed by Task 2's frontend code:
  - `lo: float` — the real 52-week low (or the estimated fallback), always the true price, never the scale-adjusted anchor.
  - `loEstimated: bool` — `True` only when the feed had no real 52-week low and this is the `min(cmp, tp) * 0.88` fallback.
  - `hi: float | None` — the real 52-week high, or `None` if the feed has none.
  - `loPct: float`, `hiPct: float | None`, `tpPct: float`, `cmpPct: float`, `addPct: float | None` — each price's 0-99 position on the rail's scale.
  - Returns `None` under the same conditions as today: no signals, or no target price.

- [ ] **Step 1: Write the failing tests**

Add these four tests to `backend/tests/test_app.py`, right after `test_give1m_survives_a_stock_that_went_to_zero`:

```python
def test_rail_shows_the_real_52w_range_not_a_silently_shifted_anchor():
    """The rail's underlying SCALE can still dip below the real 52-week low
    to make room for the Add tick -- but the LABELED low must always be the
    real price, never the scale-adjusted anchor standing in for it. This
    was the actual bug: today's loAnchor IS both the scale bound and the
    displayed label, so when Add pulls the scale down, the label silently
    shows that lower number instead of the real 52-week low."""
    h = {"tp": 4200.0, "addLvl": 3600.0}
    s = {"cmp": 2452.70, "lo": 1971.79, "hi": 3204.28}
    r = calculator.rail(h, s)
    assert r["lo"] == 1971.79
    assert r["loEstimated"] is False
    assert r["hi"] == 3204.28

    # Now push the Add level BELOW the real 52-week low -- the scale still
    # needs to dip to make room for the Add tick, but the label must keep
    # showing the real low, not the anchor.
    h2 = {"tp": 4200.0, "addLvl": 1500.0}
    r2 = calculator.rail(h2, s)
    assert r2["lo"] == 1971.79, \
        "the label must show the real 52-week low, not the anchor pulled below it for the Add tick"
    assert r2["loPct"] > 0, \
        "the real low now sits inside the scale (not pinned to the left edge), because the scale itself was pulled lower"


def test_rail_extends_the_scale_to_the_52w_high_when_its_the_highest_point():
    """Today's hiAnchor is max(TP, CMP) -- the 52-week high isn't part of
    the scale at all, so a stock trading above its old target would show
    the high clamped to the right edge as if it were near TP, when it's
    actually much higher. hiAnchor must now be max(52W high, TP, CMP)."""
    h = {"tp": 2000.0, "addLvl": None}
    s = {"cmp": 2200.0, "lo": 1800.0, "hi": 3000.0}
    r = calculator.rail(h, s)
    # 52W high (3000) is bigger than both TP (2000) and CMP (2200), so it
    # must stretch the scale rather than sit off past a scale capped at
    # 2200 -- which would show it clamped at 99%, indistinguishable from a
    # stock barely above its target.
    assert r["hiPct"] > 90, \
        "the 52-week high must genuinely stretch the scale, not just get clamped near the edge"


def test_rail_marks_a_missing_52w_low_as_estimated():
    h = {"tp": 4200.0, "addLvl": None}
    s = {"cmp": 2452.70, "hi": 3204.28}   # no "lo" in the feed
    r = calculator.rail(h, s)
    assert r["loEstimated"] is True
    assert r["lo"] == pytest.approx(min(2452.70, 4200.0) * 0.88, abs=0.01)


def test_rail_handles_a_missing_52w_high():
    h = {"tp": 4200.0, "addLvl": None}
    s = {"cmp": 2452.70, "lo": 1971.79}   # no "hi" in the feed
    r = calculator.rail(h, s)
    assert r["hi"] is None
    assert r["hiPct"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest backend/tests/test_app.py -k rail -v`
Expected: 4 failures — `KeyError: 'lo'` (or similar) since `rail()` doesn't return these keys yet.

- [ ] **Step 3: Implement the change**

Replace `backend/calculator.py:468-483` with:

```python
def rail(h, s):
    """Positions-tab price rail. None when the dashboard would show text
    instead: no feed, or no target price.

    Scale bounds and displayed points are kept separate. The scale
    (loAnchor/hiAnchor below) still dips below the real 52-week low to
    make room for the Add tick, and now also stretches above target/CMP
    to the real 52-week high when that's the biggest of the three -- but
    every DISPLAYED point (lo/hi/tp/cmp/add) is placed at its own real
    price, never silently substituted by a scale-adjustment artifact."""
    if not s:
        return None
    c, tp, add = s.get("cmp"), h.get("tp"), h.get("addLvl")
    lo, lo_estimated = s.get("lo"), False
    if lo is None and c and tp is not None:
        lo = min(c, tp) * 0.88
        lo_estimated = True
    if not (c and lo is not None and tp is not None):
        return None
    hi = s.get("hi")
    hi_a = max(tp, c, hi) if hi is not None else max(tp, c)
    lo_a = min(lo, add * 0.98) if add else lo
    span = hi_a * 1.03 - lo_a
    x = lambda v: round(min(99.0, max(0.0, (v - lo_a) / span * 100)), 1)  # noqa: E731
    return {
        "lo": round(lo, 2), "loEstimated": lo_estimated, "loPct": x(lo),
        "hi": round(hi, 2) if hi is not None else None,
        "hiPct": x(hi) if hi is not None else None,
        "tpPct": x(tp), "cmpPct": x(c), "addPct": x(add) if add else None,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest backend/tests/test_app.py -k rail -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `python -m pytest backend/tests/test_app.py -q`
Expected: all tests pass (45 existing + 4 new = 49 passed). If anything that reads the old `loAnchor`/`hiAnchor`/`span` keys breaks, that's Task 2's job to update, not a regression to chase here — confirm via `grep -rn "loAnchor\|hiAnchor" backend/ index.html` that the only other reader is `index.html` (handled in Task 2).

- [ ] **Step 6: Commit**

```bash
git add backend/calculator.py backend/tests/test_app.py
git commit -m "$(cat <<'EOF'
Separate rail scale bounds from displayed price points

rail() conflated the two: loAnchor/hiAnchor defined both the 0-99%
scale AND the displayed low/high labels. When Add needed room below
the real 52-week low, the scale (correctly) dipped to make space --
but the label silently showed that lower anchor instead of the real
low. And the scale never included the real 52-week high at all, so a
stock trading above its old target would show the high clamped near
the edge as if it were close to target.

Now the scale (loAnchor/hiAnchor internally) and the five displayed
points (lo/hi/tp/cmp/add, each with its own real price + percent
position) are separate. loEstimated flags when there's no real
52-week low and rail() fell back to the existing 0.88-of-min estimate.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Frontend — 4-band rail layout

**Files:**
- Modify: `index.html:113-129` (the `.rail` CSS block)
- Modify: `index.html:772-785` (the rail markup inside `renderPositions()`)

**Interfaces:**
- Consumes: `h.rail` from Task 1 — `{lo, loEstimated, hi, loPct, hiPct, tpPct, cmpPct, addPct}` — plus the existing `h.tp`, `h.addLvl`, `h.cmp`, `h.status.colour` (all already present on the position row object, unrelated to this change).
- Produces: nothing consumed elsewhere — this is leaf UI markup.

- [ ] **Step 1: Replace the `.rail` CSS block**

In `index.html`, replace lines 113-129 (from `.rail{position:relative;height:26px}` through the `.rail .addlbl{...}` rule) with:

```css
.rail{display:flex;flex-direction:column;gap:1px}
.rail .band{position:relative;height:13px}
.rail .band span{position:absolute;top:0;font-family:'IBM Plex Mono';font-size:9.5px;
  white-space:nowrap;line-height:13px}
.rail .lo,.rail .hi{color:var(--faint)}
.rail .tg{color:var(--saffron)}
.rail .addlbl{color:var(--blue)}
.rail .track{position:relative;height:2px;background:var(--line);margin:5px 0}
.rail .track .tick{position:absolute;top:-6px;width:2px;height:14px}
.rail .track .lotick,.rail .track .hitick{background:var(--faint)}
.rail .track .tp{background:var(--saffron)}
.rail .track .add{background:var(--blue)}
.rail .track .cmp{position:absolute;top:-3.5px;width:9px;height:9px;border-radius:50%;
  background:var(--ink);border:2px solid var(--up);transform:translateX(-50%)}
```

This drops the old absolute-stacked hack (labels floating on top of a 26px track) in favor of normal-flow rows: each band is a real row in the layout, so bands can never overlap each other — the only thing left to handle is a label running off its own band's left/right edge, done in Step 2 with a shared alignment rule.

Leave the `@media (max-width:760px)` rule at `index.html:190` (`.rail{grid-column:1 / -1;margin-top:2px}`) untouched — the rail already breaks out to full width on mobile, which is exactly what the design calls for.

- [ ] **Step 2: Replace the rail markup in `renderPositions()`**

In `index.html`, replace lines 772-785 (from `el.innerHTML = ps.positions.map(h=>{` through the closing of the `if (h.rail)` block, i.e. through `<div class="cmp" style="left:${r.cmpPct}%;border-color:${h.status.colour}"></div></div>`\`;\`) with:

```js
  el.innerHTML = ps.positions.map(h=>{
    let railHtml = `<div class="cell" style="color:var(--faint)">no price feed</div>`;
    if (h.rail){
      const r = h.rail;
      // Shared edge-clamp rule for every rail label: hug the left edge
      // near 0%, hug the right edge near 100%, centered in between -- so
      // no label can ever run off the rail regardless of which of the
      // five prices it's attached to.
      const lab = (pct, cls, text, style) => pct==null ? "" :
        `<span class="${cls}" style="left:${pct}%;transform:translateX(${pct<15?"0":pct>85?"-100%":"-50%"})${style?";"+style:""}">${esc(text)}</span>`;
      const loTxt = "52W LOW " + fmt.inr(r.lo) + (r.loEstimated ? " (est.)" : "");
      railHtml = `<div class="rail">
        <div class="band">
          ${lab(r.loPct, "lo", loTxt)}
          ${r.hiPct!=null ? lab(r.hiPct, "hi", "52W HIGH " + fmt.inr(r.hi)) : ""}
        </div>
        <div class="band">${lab(r.tpPct, "tg", "TP " + fmt.inr(h.tp))}</div>
        <div class="track">
          <div class="tick lotick" style="left:${r.loPct}%"></div>
          ${r.hiPct!=null ? `<div class="tick hitick" style="left:${r.hiPct}%"></div>` : ""}
          ${r.addPct!=null ? `<div class="tick add" style="left:${r.addPct}%" title="Add level"></div>` : ""}
          <div class="tick tp" style="left:${r.tpPct}%"></div>
          <div class="cmp" style="left:${r.cmpPct}%;border-color:${h.status.colour}"></div>
        </div>
        <div class="band">${lab(r.cmpPct, "cmplbl", "CMP " + fmt.inr(h.cmp), "color:"+h.status.colour)}</div>
        ${r.addPct!=null ? `<div class="band">${lab(r.addPct, "addlbl", "ADD LEVEL " + fmt.inr(h.addLvl))}</div>` : ""}
      </div>`;
    } else if (h.cmp && h.tp==null){
      railHtml = `<div class="cell" style="color:var(--faint)">CMP ${fmt.inr(h.cmp)} · no target set</div>`;
    }
```

(The rest of `renderPositions()` — from `const zc = v => ...` through the end of the function — is unchanged.)

- [ ] **Step 3: Start the local dev server and preview the Positions tab**

```bash
# Use the Claude Browser tools' preview_start with name "dl-india-core"
# (already configured in .claude/launch.json from earlier this session),
# then navigate to the Positions tab.
```

Confirm visually (via `read_page` or `get_page_text`) that a holding with a target price shows all five labels: `52W LOW ₹X`, `52W HIGH ₹Y` (when the feed has one), `TP ₹Z`, `CMP ₹C`, `ADD LEVEL ₹W` (when an add level is set).

- [ ] **Step 4: Run the same overflow/collision scan used for the two prior UI fixes this session**

Via `javascript_tool`, at both mobile (375px, via `resize_window`) and desktop widths, on the Positions tab:

```js
function scanRailOverlaps() {
  const rails = [...document.querySelectorAll('.rail')];
  const results = [];
  for (const rail of rails) {
    const spans = [...rail.querySelectorAll('span')];
    for (const sp of spans) {
      if (sp.scrollWidth > sp.clientWidth + 1) {
        results.push({tk: rail.closest('.row')?.querySelector('.tk')?.textContent, text: sp.textContent, issue: 'text overflows its own span'});
      }
    }
    // Two spans in the SAME band colliding horizontally (only realistic
    // risk left after the 4-band split, if the whole book has enough
    // holdings where 52W low and 52W high land very close together).
    const bands = [...rail.querySelectorAll('.band')];
    for (const band of bands) {
      const bspans = [...band.querySelectorAll('span')];
      for (let i = 0; i < bspans.length; i++) {
        for (let j = i+1; j < bspans.length; j++) {
          const a = bspans[i].getBoundingClientRect(), b = bspans[j].getBoundingClientRect();
          if (!(a.right < b.left || a.left > b.right)) {
            results.push({tk: rail.closest('.row')?.querySelector('.tk')?.textContent, issue: 'two labels in the same band overlap', a: a.left, b: b.left});
          }
        }
      }
    }
  }
  return results;
}
JSON.stringify(scanRailOverlaps())
```

Expected: `[]` (empty array) at both widths. If the range-band collision case fires (52W low and high landing close together), that's a real edge case to fix before moving on — narrow the `lab()` clamp thresholds or stagger the two spans vertically within the band.

- [ ] **Step 5: Run the full backend test suite one more time (sanity check nothing server-side broke)**

Run: `python -m pytest backend/tests/test_app.py -q`
Expected: 49 passed (unchanged from Task 1's Step 5 — this task touches no Python).

- [ ] **Step 6: Commit**

```bash
git add index.html
git commit -m "$(cat <<'EOF'
Redesign the Positions rail as a 4-band ladder

Every concept -- range (52W low/high), TP, CMP, Add level -- now gets
its own row instead of being crammed onto a single 26px track via
absolute-position overlaps. Concretely fixes three things: the real
52-week low/high are now always shown (previously the low label could
silently show a scale-adjusted anchor instead, and the high wasn't
shown at all), CMP gets a visible price label instead of just an
unlabeled dot, and Add reads as "ADD LEVEL <price>" instead of a bare
"Add <price>".

Because each band belongs to exactly one concept, nothing on one band
can ever collide with anything on another -- the only remaining risk
is a label running off its own band's edge, handled by a single
shared clamp rule reused by all five labels instead of five separate
ad-hoc ternaries.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Full regression pass and push

**Files:** none (verification + publish only)

**Interfaces:** none

- [ ] **Step 1: Run the full backend suite one final time**

Run: `python -m pytest backend/tests/test_app.py -q`
Expected: 49 passed, 0 failed.

- [ ] **Step 2: Run `build_signals.py --selftest`**

Run: `python scripts/build_signals.py --selftest`
Expected: `selftest ok` — this task doesn't touch `build_signals.py`, this just confirms the working tree is otherwise clean/consistent before pushing.

- [ ] **Step 3: Re-run the overflow/collision scan from Task 2 Step 4 across all six tabs**

Same `scanOverlaps`-style scan used for the two prior UI fixes this session (`scrollWidth > clientWidth` across every leaf element in the active view), at both 375px and desktop widths, cycling through all six tabs (`ov`, `exp`, `risk`, `perf`, `posn`, `imp`) via `showView(id)`. Expected: `[]` on every tab at every width — confirms the taller rail didn't push anything else on the Positions row into an overflow state.

- [ ] **Step 4: Push both commits to both remotes**

```bash
git push origin main
git push core main:master
```

- [ ] **Step 5: Report back**

Summarize to the user: what changed, screenshot or text-dump of a real holding's rail (e.g. TCS) showing all five labels, and confirmation that the full test suite + overflow scan are clean.
