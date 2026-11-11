# jCO builder templates

Builder templates for this repo, in the jCO spec (`jtalborough/agents`, `JCO.md`). The Magpie lead (`jtalborough/agents` `agents/magpie-lead`) starts them as short-lived tasks.

- `builders/magpie-code/`: software (app, API, web UI, tests, Supabase).
- `builders/magpie-cabinet/`: the physical cabinet (LED controller, storage layout, edge devices, 3D models).

Builders have no soul and no private memory. They never merge. No secrets here: this repo is public.
Check: `agent validate --builders .` from a checkout of `jtalborough/agents`.
