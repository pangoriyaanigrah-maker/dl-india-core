# Positions rail redesign — 4-band ladder

## Problem

The Positions-tab price rail packs 52W low, target price (TP), CMP, and Add
level onto a cramped 26px track, and two of those are wrong or missing
outright:

1. The label shown as the rail's low end (`loAnchor`) is not always the real
   52-week low. `rail()` pulls it down to `min(52w_low, addLevel * 0.98)` so
   the Add tick has room to sit below the track — when Add is below the real
   low, the label silently shows that lower, unlabeled number instead.
2. The 52-week high isn't shown anywhere. The rail's right edge is anchored
   at `max(TP, CMP)`, with no reference to the actual 52-week high at all.
3. CMP is drawn as an unlabeled colored dot on the track — no price text.
4. The Add level's label ("Add ₹2,400") doesn't read as a distinct, named
   price level next to TP's clearly-labeled one.

## Goals

- Always show the real 52-week low and high as their own labeled points,
  never silently substituted by a scale-adjustment artifact.
- Give CMP a visible price label tied to its marker.
- Reword the Add label so it reads as a named level, consistent with the
  other four.
- Fit five labeled points on the rail without any of them colliding, at any
  screen width, without hiding the info that was explicitly asked for.

## Non-goals

- No change to how TP, Add level, or 52W low/high are computed upstream
  (holdings file, signals feed) — this is a display and scale-math fix, not
  a new data source.
- No change to the scenario ladder, liquidity table, or any other Risk-tab
  content — scope is the Positions-tab rail only.

## Design

### Data model — `rail()` in `backend/calculator.py`

Today `loAnchor`/`hiAnchor` serve double duty: they define the rail's 0–99%
scale *and* they are what the frontend labels as "the low" / implicitly "the
high" (which isn't shown at all). That conflation is the root cause of
problem #1.

Split scale bounds from displayed points:

- **Scale bounds** (`loAnchor`, `hiAnchor`, `span`) — unchanged in spirit,
  one extension:
  - `loAnchor = min(52w_low, addLevel * 0.98)` when Add is set, else
    `52w_low` — same as today, still needs room for the Add tick.
  - `hiAnchor = max(52w_high, TP, CMP)` — **extended** to include the real
    52-week high (today it's `max(TP, CMP)` only), so a stock trading above
    its old target still shows the high honestly positioned to scale.
  - `span = hiAnchor * 1.03 - loAnchor` — unchanged (3% headroom).
- **Displayed points** — five independent `{price, pct}` pairs, each placed
  at `x(price)` on the same scale: `lo` (real 52W low, with an
  `loEstimated: bool` flag set when the feed had no real 52W low and it
  fell back to the existing `min(cmp, tp) * 0.88` estimate), `hi` (real 52W
  high), `tp`, `cmp`, `add`.

`rail()` returns `None` under the same conditions as today (no feed, or no
target price) — unchanged.

### Layout — 4 bands, top to bottom

Each concept gets its own horizontal band, so nothing on one band can ever
collide with anything on another band. The only remaining collision risk is
a label running off its own band's left/right edge, handled the same way
today's TP label already flips its alignment near the edge (mirror that
existing pattern for all five labels instead of adding a new one).

1. **Range band**: `52W LOW ₹X` (or `~52W LOW ₹X (est.)` when
   `loEstimated`) and `52W HIGH ₹Y`, each positioned at its own `pct` — not
   pinned to the corners, since either can sit inside the range once TP or
   Add exceeds it.
2. **TP band**: `TP ₹Z`, saffron — same styling as today.
3. **Track**: horizontal line with tick marks for Low, High, Add, TP (thin,
   colored per concept) and a filled dot for CMP (ring colored by thesis
   status, same as today).
4. **CMP band**: `CMP ₹C`, colored to match the dot's status ring.
5. **Add band**: `ADD LEVEL ₹W`, blue.

### Responsive behavior

The rail currently shares a narrow half-width column with a neighboring
cell on mobile (`.row-main{grid-template-columns:1fr 1fr}`). It becomes a
full-width block on mobile — breaking out of that 2-column grid — so all
five bands have room; other row cells stay 2-column above/below it. Same
4-band design at every width; no separate mobile-only layout to maintain.

### Error handling

Unchanged from today: no price feed → "no price feed" text cell; a target
price but no feed price, or vice versa → existing text fallbacks. Both
bypass the rail (and the new band logic) entirely, same as now.

## Testing

- One `pytest` case in `backend/tests/test_app.py` covering:
  - `hiAnchor` extends to the 52-week high when it exceeds both TP and CMP.
  - The real 52-week low and high come back undistorted even when Add pulls
    `loAnchor` below the real low.
  - `loEstimated` is `True` only when the feed has no real 52-week low.
- Frontend verified visually via the local dev-server preview (mobile +
  desktop viewports, all six tabs checked for overflow/collisions), the
  same verification pattern used for the two prior UI fixes this session.
