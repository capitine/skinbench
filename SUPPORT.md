# Support

SkinBench support is best effort. There is no response-time promise and no
promised release schedule. Support covers the latest public beta only.

## Before opening an issue

1. Confirm the problem on the latest beta.
2. Work in a disposable scene with no proprietary content.
3. Read the [User Guide](docs/user-guide.md), [Safety](docs/safety.md), and
   [Compatibility](docs/compatibility.md) pages.
4. Search [existing issues](https://github.com/capitine/skinbench/issues).

Use [GitHub Issues](https://github.com/capitine/skinbench/issues) for ordinary
bugs, focused feature requests, and compatibility reports. Use private
vulnerability reporting for security problems as described in
[SECURITY.md](SECURITY.md).

## Compatibility report checklist

Every compatibility report must name:

- Maya version;
- operating system;
- install result;
- panel-open result;
- one safe operation that was tested.

A safe operation can be opening the panel, refreshing from a disposable
selection, running a check or preview that does not write weights, or running a
documented self-test in an empty mayapy session.

Do not attach proprietary rigs, scenes, client data, credentials, logs with
private paths, or skin-weight snapshots. Build a small disposable reproduction
instead.
