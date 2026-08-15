# Contribute to the project

Contributions and issues are most welcome! All issues and pull requests are
handled through [GitHub](https://github.com/gilesknap/podbench/issues). Also, please check for any existing issues before
filing a new one. If you have a great idea but it involves big changes, please
file a ticket before making a pull request! We want to make sure you don't spend
your time coding something that might not fit the scope of the project.

## Issue or Discussion?

Github also offers [discussions](https://github.com/gilesknap/podbench/discussions) as a place to ask questions and share ideas. If
your issue is open ended and it is not obvious when it can be "closed", please
raise it as a discussion instead.

## Code Coverage

While 100% code coverage does not make a library bug-free, it significantly
reduces the number of easily caught bugs! Please make sure coverage remains the
same or is improved by a pull request!

## Developer Information

It is recommended that developers use a [vscode devcontainer](https://code.visualstudio.com/docs/devcontainers/containers). This repository contains configuration to set up a containerized development environment that suits its own needs.

`Charts/podbench/values.schema.json` is **generated, never hand-edited**: a
pre-commit hook regenerates it from `values.yaml` and `example.values.yaml`. That
hook is a shim around a helm plugin — the devcontainer image installs both it and
`helm`, and a checkout outside the devcontainer needs
`helm plugin install https://github.com/losisin/helm-values-schema-json --version
v2.5.0` before `pre-commit run --all-files` will pass — pinned to the hook's
`rev`, because a different plugin version generates a different schema and the
disagreement lands in CI as a diff nobody wrote. Add a value by editing
`values.yaml` —
including the `# @schema` comment if it needs an enum or an item shape — and
letting the hook rewrite the JSON.

This project was created using the [Diamond Light Source Copier Template](https://github.com/DiamondLightSource/python-copier-template) for Python projects.

For more information on common tasks like setting up a developer environment, running the tests, and setting a pre-commit hook, see the template's [How-to guides](https://diamondlightsource.github.io/python-copier-template/5.4.0/how-to.html).
