# Contributing

Thanks for helping improve Claber BT.

## Development

1. Clone this repository into your Home Assistant `config` tree, or symlink `custom_components/claber_bt`
2. Restart Home Assistant after Python changes
3. Hard-refresh the browser after card (`www/claber-card.js`) changes. The URL is cache-busted by integration version.

### Layout

- `custom_components/claber_bt/`: integration (includes `www/claber-card.js`)
- `card/`: card harnesses and source assets used while developing the card
- `tests/`: pytest unit tests that do not require a live device
- `docs/images/`: README screenshots

### Tests

```bash
pip install pytest
pytest -q
```

CI also runs [hassfest](https://github.com/home-assistant/actions) and [HACS validation](https://github.com/hacs/action).

## Guidelines

- Do not change device protocol or command byte semantics without a clear rationale and tests
- Keep valve and program commands manual-only. Do not add automations that open valves on their own.
- Prefer passive BLE for state; GATT only on explicit user action
- Scrub secrets and personal identifiers (MACs from private installs, hostnames, tokens) before committing
- Card copy: English default, Italian when `hass.language` / locale is `it`; keep the UI minimal (no redundant labels)

## Pull requests

- Target `main`
- Include a short summary of behaviour changes
- Add or update tests when touching parsing, irrigation end-time logic, or codecs
- Update `CHANGELOG.md` under an Unreleased or version section when user-facing behaviour changes

## Issues

Use the GitHub issue templates for bugs and feature requests. Include Home Assistant version, Bluetooth path (built-in adapter vs ESPHome proxy), and redacted diagnostics when possible.
