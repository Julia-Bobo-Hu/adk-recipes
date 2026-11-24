# A2UI schemas (test fixtures)

Three JSON Schema files published by the A2UI project. `tests/test_chat_surfaces.py`
validates every message built by `scripts/a2ui_cards.py` against them. Nothing at run
time reads these files.

| File | Published as | Source page |
| :-- | :-- | :-- |
| `server_to_client.json` | `https://a2ui.org/specification/v0_9/server_to_client.json` | https://a2ui.org/specification/v0.9.1/schemas/ |
| `common_types.json` | `https://a2ui.org/specification/v0_9/common_types.json` | https://a2ui.org/specification/v0.9.1/schemas/ |
| `basic_catalog.json` | `https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json` | https://a2ui.org/specification/v0.9.1-basic-catalog-implementation-guide/ |

- Retrieved on 2026-11-20 from the pages above. The content is unchanged; the files are
  re-serialised with one-space indentation.
- Licence: Apache-2.0, the licence of the A2UI repository
  (https://github.com/a2ui-project/a2ui, `LICENSE`).
- These are the v0.9.1 schemas. They keep `v0_9` in their `$id` values and accept
  `"version": "v0.9"` and `"v0.9.1"`; the cards send `"v0.9"`.
- The envelope schema refers to its catalog by the placeholder file name `catalog.json`.
  The specification says to map that name to the catalog in use, and the test maps it to
  `basic_catalog.json`.

To refresh: download the three schemas again, replace the files, and run the tests.
