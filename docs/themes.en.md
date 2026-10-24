# Custom themes

[Русская версия](themes.md)

The panel ships with two themes: Night (id `new`) and Classic (id `classic`). A theme is nothing more than a set of CSS variables (tokens) in one file, `static/tailwind.css`. There is no build step: edit the file, reload the page, and you see the result.

You can go two ways. Recoloring an existing theme is quick and touches one file. Adding a third theme to the picker takes longer, because its id has to be registered in a few more places (listed below).

## How themes work

The top of `static/tailwind.css` has two blocks:

```css
:root,
:root[data-theme="classic"] { /* Classic tokens */ }

:root[data-theme="new"] { /* Night tokens */ }
```

The server puts the user's theme id into the `data-theme` attribute on `<html>`, and the browser picks the values from the matching block. Every component (header, cards, buttons, chips, forms) reads only variables, so changing one token recolors it everywhere.

The one rule: themes differ **only in token values**. Do not write rules like `[data-theme="new"] .card { … }`. Overrides of that kind broke the old themes, and `tests/test_theme.py` rejects them.

Classic sits on `:root`, which makes it the fallback. Any token your theme does not set is taken from Classic, so a new theme can start with a dozen variables.

## What each token controls

| Group | Tokens | Where you see it |
|---|---|---|
| Page | `--bg`, `--bg-image*`, `--on-bg`, `--on-bg-muted` | background behind the work column and text placed directly on it |
| Header | `--top`, `--top-border`, `--top-text`, `--top-muted`, `--top-tab` | the top bar with tabs |
| Surfaces | `--surface`, `--surface-2…4`, `--panel`, `--page-surface` | ticket cards, forms, admin panels |
| Text | `--text`, `--text-soft`, `--text-muted`, `--text-faint`, `--placeholder` | main and secondary text on cards |
| Fields | `--field`, `--field-text`, `--border*` | inputs, borders, focus ring |
| Primary buttons | `--btn-primary`, `--btn-primary-hover`, `--on-primary` | "New", "Save", "Sign in" |
| Close button | `--btn-success`, `--btn-success-hover`, `--on-success` | closing a ticket |
| Urgency | `--stripe-overdue`, `--stripe-soon`, `--stripe-later`, `--stripe-none`, `--stripe-closed` | the colored stripe on the left of a card |
| Chips | `--chip-*` (`red`, `amber`, `green`) | the time chip, statuses |
| Number | `--num-bg`, `--num-text`, `--count-bg`, `--count-text` | the `#108` badge, tab counters |
| Home | `--menu-*`, `--hero-*`, `--raccoon` | the three menu buttons, sign-in and home background, whether the raccoon is shown |
| Column | `--page-max`, `--page-surface`, `--page-radius` | the work column with the ticket list |

Corner radii (`--r-card`, `--r-btn` and friends) are not part of a theme. The separate "Rounded / Square" setting in admin and in the profile controls them.

## Way 1: recolor an existing theme

Say you want a purple accent in Night instead of teal:

1. Open `static/tailwind.css` and find the `:root[data-theme="new"]` block.
2. Change the accent tokens: `--accent`, `--btn-primary`, `--btn-primary-hover`, `--brand`, `--menu-icon`, `--count-bg`, `--border-focus`. If the button got light, adjust `--on-primary`, the text color on it.
3. In `templates/base.html`, bump the stylesheet version: `tailwind.css?v=52` becomes `?v=53`. Otherwise browsers keep showing the cached file.
4. Reload the page.

Check text contrast. Regular text needs at least 4.5:1. DevTools makes this easy: click a color in the Styles panel and the browser shows the ratio.

## Way 2: add a new theme

Say you want a "Forest" theme with the id `forest`.

**1. Tokens.** In `static/tailwind.css`, after the Night block, add:

```css
:root[data-theme="forest"] {
    color-scheme: dark;
    --bg: #0d1a12;
    --on-bg: #e6f2ea;
    --top: rgb(13 26 18 / 0.94);
    --surface: #15281c;
    --text: #e6f2ea;
    --btn-primary: #4caf50;
    --btn-primary-hover: #43a047;
    --on-primary: #08130c;
    /* everything else comes from Classic; add tokens as you need them */
}
```

Then the swatch for the profile picker, next to `.swatch-new`:

```css
.swatch-forest { background: linear-gradient(135deg, #0d1a12 50%, #4caf50 50%); }
```

**2. Allow the id on the server.** In `app/sync.py`, add the theme to the tuple:

```python
VALID_THEMES = ("new", "classic", "forest")
```

The server refuses to store unknown ids and shows the default theme instead, so without this step nobody can pick the theme.

**3. Header toggle.** In `static/theme.js`, add the id to the cycle order and labels in both languages:

```js
var ORDER = ['new', 'classic', 'forest'];
var LABELS = EN ? { new: 'Night', classic: 'Classic', forest: 'Forest' }
                : { new: 'Ночь', classic: 'Классика', forest: 'Лес' };
```

**4. Pickers.** The theme is chosen in two places:

- `templates/profile.html`: add `('forest', _("Лес"))` to the list `[('new', _("Ночь")), ('classic', _("Классика"))]`;
- `templates/admin/index.html`: add `<option value="forest"…>{{ _("Лес") }}</option>` to the "Default theme" select, following the existing options.

UI strings are written in Russian inside `_()` and translated in a catalog, which is why the label above is `_("Лес")`.

**5. Translation.** Add `"Лес": "Forest"` to `app/locales/en.json`. `tests/test_i18n.py` fails if a translation is missing.

**6. File versions.** In `templates/base.html`, bump `tailwind.css?v=…` and `theme.js?v=…`.

**7. Tests.** The theme list is pinned in tests so a theme cannot disappear by accident. Add the id to `THEMES` and to the `ORDER` line in `tests/test_theme.py`, and to the `VALID_THEMES` check in `tests/test_default_theme.py`. Then run:

```bash
make check
```

The smoke test opens every page in every theme and checks that it renders.

## Images: background and raccoon

In Night, the sign-in and home background (`--hero-image`, `--backdrop`) points to `static/hero-bg.webp`, and `--raccoon: block` shows the raccoon from `static/raccoon-hero.webp`. In your theme you can:

- use your own picture: put the file into `static/` and reference it in `--hero-image` and `--backdrop` with `url('/static/name.webp')`;
- hide the raccoon: `--raccoon: none`;
- skip images entirely, like Classic does: `--hero-image: var(--bg-image)`.

Compress images to WebP. The Night background is about 83 KB at 1672 px wide. On phones (up to 44rem) Night uses a smaller `hero-bg-900.webp`, about 35 KB: `--hero-image` and `--backdrop` are redefined in a media query after the theme blocks. Do the same if your background is heavy.

## Troubleshooting

- The theme does not apply: check that the id is the same everywhere (CSS, `VALID_THEMES`, `theme.js`, pickers). The server silently replaces an unknown id with the default theme.
- The colors are stale: you did not bump `?v=` in `base.html` and the browser is using its cache. A hard reload (Cmd+Shift+R) also helps.
- Some element kept its old color: it reads a token you did not set, so it gets the Classic value. Find the element in DevTools and see which variable it uses.
