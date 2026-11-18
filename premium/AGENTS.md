# Premium API in the shared repository

- The repository root hosts the legacy API and its original startup files.
- Premium code, tests, dependencies, Docker configuration and brand assets live in this directory.
- Run premium commands with this directory as the working directory.
- Keep service credentials, customer documents, databases and runtime snapshots in private storage outside Git and builds.
- Real customer PDF acceptance uses HTTP and a separate worker. Synthetic tests may use an explicit fake provider.
- Preserve the completed/uncertain provider ledger and use cached rebuilds for layout changes.
- The Bubble integration requires separate authorization. Inspect it read-only until that authorization is given.
