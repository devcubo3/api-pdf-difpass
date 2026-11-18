# Premium API in the shared repository

- The repository root hosts the legacy API and its original startup files.
- Premium code, tests, dependencies, Docker configuration and brand assets live in this directory.
- Run premium commands with this directory as the working directory.
- The user chose ONE existing Dokploy service and public hostname on 2026-11-18. Use Dockerfile.shared with repository-root build context for opt-in activation. Preserve the original root entry points for rollback.
- Shared mode has isolated legacy/premium dependency environments, a supervised worker and a persistent volume with a single-instance lock. Keep one replica, stop-first updates and Chromium sandboxing. Verify the hosting runtime's per-service seccomp support before activation; never disable sandboxing as a fallback.
- Keep service credentials, customer documents, databases and runtime snapshots in private storage outside Git and builds.
- Real customer PDF acceptance uses HTTP and a separate worker. Synthetic tests may use an explicit fake provider.
- Preserve the completed/uncertain provider ledger and use cached rebuilds for layout changes.
- The Bubble integration requires separate authorization. Inspect it read-only until that authorization is given.
