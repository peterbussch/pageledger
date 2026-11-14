# Security

## Reporting a problem

Please report security problems privately, not in a public issue. Use GitHub's
private vulnerability reporting: on the repository's **Security** tab, choose
**Report a vulnerability**. If that button is missing, open an issue that says
only that you have a security report and asks for a private way to send it;
leave the details out.

Say which PageLedger version you used (`pageledger --version`), what you ran,
and what happened. A small job, run or bundle folder that shows the problem
helps most. You will get an answer, and a fix is released as soon as it is
ready, with credit if you want it.

Only the latest release is supported. A fix goes into a new release, not into
older ones.

## What counts

PageLedger reads documents and folders it did not make, so these are security
problems:

- a job, run or bundle folder that makes `resume`, `replay` or any other
  command run code, or send page images or a key to a server, without the
  `--adapter-path` or `--trust-endpoint` that the rules below require;
- a received folder that makes a read-only command (`verify`, `inspect`,
  `export`, `review`, `compare`) run code, write outside the folders it
  names, or hang or exhaust memory;
- a crafted PDF, image, IIIF manifest, ALTO file or review sheet that does
  any of these;
- a review view (`review --out`) that loads anything from the network or runs
  a page's text as code;
- `fetch` reaching a private network address that the user did not name, or
  writing a key or password into a record;
- a changed file in a job or run that `verify` does not catch, when the change
  is not a consistent rewrite of the whole record (see below).

These are not security problems, though they may be bugs:

- an OCR or model reading that is wrong, or text a model made up: that is what
  holds and review are for;
- a job or run that someone rewrote consistently, recomputing every hash with
  PageLedger's own code. `verify` checks that a folder agrees with itself, not
  who wrote it; publish the hash of `job.json` or deposit with a repository that
  timestamps deposits to anchor it;
- anything a custom adapter, or a configuration you wrote yourself, does: your
  own `--config` and `--adapter-path` are trusted as written;
- the known limits listed in
  [capabilities and limits](docs/capabilities-and-limits.md#known-limits).

## The trust model for folders you receive

Looking at a job, run or bundle runs none of its code. Continuing one does: a
folder's configuration names the engines to run, and may name its own Python
code or a model server. So `resume` and `replay` run code a folder names only
from a folder you give with `--adapter-path`, never from the received folder
itself, and send pages to a server off this machine only if you name it with
`--trust-endpoint`. Do not resume a job, or replay a bundle, that someone sent
you unless you trust where it came from.
[Jobs, runs and bundles you receive](docs/share-and-cite.md#jobs-runs-and-bundles-you-receive)
explains this for readers.
