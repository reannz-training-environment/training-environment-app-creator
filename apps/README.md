# Apps

One YAML file per app, named after the app: `apps/<name>.yml`. The
[app creator website](https://reannz-training-environment.github.io/training-environment-app-creator-website/)
writes these and opens the pull request; they can also be written by hand,
following [`schema/app.schema.json`](../schema/app.schema.json) and the
[examples](../examples).

Merging a new or changed file here creates or updates the app's repositories,
one per interface: `training-environment-<interface>-<name>-app`.

Deleting a file here does not delete anything: the app's repositories stay
until someone archives or deletes them.
