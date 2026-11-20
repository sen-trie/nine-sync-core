# i18n

UI translations for NineSync: PMM Bridge.

- `ui.pot`: the template, extracted from the source (labels, tooltips, report messages).
- `locales/<locale>.po`: one catalog per language (`ja_JP`, `zh_HANS`). Edit only these.
- `translations.py`: generated and committed, registered by `__init__.py`. Never edit it by hand.
- `__init__.py`: package marker.

Run the tool from the release checkout (the superproject, with submodules):

```
python packaging/i18n_tool.py audit    --domain core    # call sites Blender cannot translate as written
python packaging/i18n_tool.py extract  --domain core    # source -> i18n/ui.pot
python packaging/i18n_tool.py merge    --domain core    # ui.pot -> every locales/*.po
python packaging/i18n_tool.py compile  --domain core    # .po -> translations.py
python packaging/i18n_tool.py check    --domain core
python packaging/i18n_tool.py coverage --domain core
```

An entry marked `#, fuzzy` is a draft: it is kept in the `.po` but not compiled, so Blender shows the English text until a native speaker reviews it and removes the flag.

See https://github.com/sen-trie/nine-sync-release/blob/main/DEVELOPER_GUIDE.md#translations
