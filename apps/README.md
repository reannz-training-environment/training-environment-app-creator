# Apps

One YAML file per app, named after the app: `apps/<name>.yml`. The
[app creator website](https://reannz-training-environment.github.io/training-environment-app-creator-website/)
files a request, and the app creator writes the file and opens the pull
request. They can also be written by hand, following
[`schema/app.schema.json`](../schema/app.schema.json) and the
[examples](../examples).

Merging a new or changed file here creates or updates the app's repositories,
one per interface: `training-environment-<interface>-<name>-app`, and builds
and releases their images.

To delete an app, run the **Delete an app** workflow (*Actions*, *Delete an
app*, *Run workflow*): it deletes the app's file here, its repositories and
their images. Deleting a file here by hand deletes nothing else.
