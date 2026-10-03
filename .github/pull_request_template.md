<!--
Pull requests from the app creator website add or change one file in apps/.
The Validate workflow comments with the repositories merging will create or
update, and test-builds every image.
-->

## App

<!-- What workshop is this for, and when is it running? -->

## Before merging

- [ ] The Validate checks pass: the spec is valid and every image builds
- [ ] The app name is right: it becomes the repository and image names, which cannot be changed later
- [ ] Any `advanced.dockerfile` or `advanced.startup` commands have been read and are safe
- [ ] Data sources are public, and pinned to a commit or tag where possible
- [ ] For a change to an existing app, `version` has been bumped if the change should be released
