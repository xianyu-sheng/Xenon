# Xenon Star Core v2 — visual proposal

Approved as Xenon's visual identity and integrated into the public logo,
terminal startup animation, and terminal-tab activity states.

## Identity

- **Primary silhouette:** an eight-ray xenon discharge instead of a rounded square.
- **Brand core:** `Xe` is present at large sizes; below 48 px the unlettered star glyph is used.
- **Constellation:** four uneven satellite stars and broken orbital arcs communicate multi-model routing without making the icon look like a diagram.
- **Palette:** near-black space, xenon cyan, electric blue, and a restrained violet edge.

## Runtime states

The image asset and the terminal tab title are two renderers of the same state model:

| State | Tab title | Motion |
|---|---|---|
| idle / task complete | `✶·· Xenon` | still |
| model or tool running | `✶··` → `·✶·` → `··✶` | 4 fps loop |
| waiting for permission / user input | `☆·· Xenon · 等待确认` | still |
| interrupted | `☆·· Xenon · 已中断` | still |

The three-character star field remains the same width in every animation frame, so terminal tabs do not jitter. A plain ASCII fallback (`*..`, `.*.`, `..*`) should be available for fonts without the star glyph.

## Implementation boundary

Most terminals do not expose a portable API for replacing the tab's bitmap icon frame by frame. Xenon can portably animate the **tab title** through OSC 0/2, while desktop/Dock icons remain static. Terminal-specific bitmap integrations can be added later as optional adapters.
