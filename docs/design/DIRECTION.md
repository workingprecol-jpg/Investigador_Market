# Final Boss — light operational dashboard

The user selected Interface Design and explicitly excluded dark mode. The local
source snapshot, license and source hashes are in `interface-design/`.

## Working brief

The owner monitors one Windows paper/demo crypto bot, wants to understand what
the coordinator and six specialists actually do, and needs to recognize stale
data without inspecting logs. Product copy remains Spanish.

Domain: closed candles, signal consensus, risk vetoes, protected positions,
funding costs, decision attribution, daily review and prospective experiments.
Color world: warm paper, white analytical surfaces, graphite ink, emerald
confirmation, amber caution and muted red loss/error. Semantic colors describe
outcomes and states; they are not interchangeable decoration.

The signature is a connected agent workspace: each module has an actual task,
instrument, result and timestamp beside the resulting decision stream. Its
activity comes from runtime events. Short computations appear as completed
events; waiting does not animate as processing. The sixth specialist normally
shows its next scheduled review.

Use a compact workstation composition: persistent navigation, a terse continuity
bar, a portfolio summary, a prominent agent workspace, a market/candlestick view,
positions, decision history and daily research. Group by the owner's decisions
instead of filling the page with identical cards. No decorative probability
graphs, invented agent thoughts, fake progress percentages or simulated profits
presented as account results.

The news and purchase-decision area uses the same read-only workspace language:
source status, publication time, symbol relevance and the last recorded
buy/wait/no-buy label. A stale cycle visually resolves to wait rather than
reusing an earlier buy signal. Headlines are evidence, not a probability badge.

## System decisions

- Light-only CSS, including when the OS requests dark mode; no theme switch.
- A four-pixel spacing base; compact control rows, 12–20px analytical surfaces,
  and generous separation between distinct work areas.
- Segoe UI for familiar Windows reading; tabular numbers for quotes, balances,
  timers and tables. Hierarchy comes from weight, scale and selective emphasis.
- White surfaces on warm near-white, restrained borders and subtle elevation.
- Native accessible buttons, selects and disclosure controls. Visible focus,
  readable contrast, reduced-motion support and usable mobile layouts.
- A local, read-only HTTP dashboard receives a JSON snapshot every two seconds.
  It retains useful data during reconnection and labels old market/agent data.
  Refreshing the server response never makes a bot heartbeat newer.
- No controls that place orders, change leverage, adjust risk or reset balances.
- Existing paper/demo history and financial logic remain the source of truth.

## Verification

Check the independent JSON projection, request isolation, freshness rules,
failure containment of telemetry, client filtering and rendering, keyboard
controls, desktop/mobile layout and both active and empty/error states.
