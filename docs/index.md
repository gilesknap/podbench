---
html_theme.sidebar_secondary.remove: true
---

```{include} ../README.md
:end-before: <!-- README only content
```

Where to start
--------------

* Never used it — [Installation](tutorials/installation.md), then
  [Your first session](tutorials/first-session.md).
* Here to debug a crash — [Debug with gdb](how-to/debug-with-gdb.md).
* Here to change code in the cluster — [Iterate on Python](how-to/iterate-on-python.md).
* Here to decide whether to allow it — [Security model](explanations/security.md).
* Looking for a flag — [Command-line reference](reference/cli.md).
* Met a word you do not know — [Glossary](reference/glossary.md).

How the documentation is structured
-----------------------------------

Documentation is split into [four categories](https://diataxis.fr), also accessible from links in the top bar.

<!-- https://sphinx-design.readthedocs.io/en/latest/grids.html -->

::::{grid} 2
:gutter: 4

:::{grid-item-card} {material-regular}`directions_walk;2em`
```{toctree}
:maxdepth: 2
tutorials
```
+++
Tutorials for installation and typical usage. New users start here.
:::

:::{grid-item-card} {material-regular}`directions;2em`
```{toctree}
:maxdepth: 2
how-to
```
+++
Practical step-by-step guides for the more experienced user.
:::

:::{grid-item-card} {material-regular}`info;2em`
```{toctree}
:maxdepth: 2
explanations
```
+++
Explanations of how it works and why it works that way.
:::

:::{grid-item-card} {material-regular}`menu_book;2em`
```{toctree}
:maxdepth: 2
reference
```
+++
Technical reference material including APIs and release notes.
:::

::::
