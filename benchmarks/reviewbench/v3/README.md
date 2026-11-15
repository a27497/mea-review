# ReviewBench v3 offline adjudication

Use a prepared blind packet from your private evidence storage. Its manifest
pins the ordered candidate pages and referenced sources. Keep the original
packet and its judge-only mapping separately. The reader checks packet hashes
and keeps decisions in a separate owner draft.

```sh
python3 -I -B benchmarks/reviewbench/v3/holdout_adjudicate.py serve --packet /path/to/packet --draft /path/to/owner-draft.json
```

The reader listens on loopback. Use the same arguments with `tty` for an
interactive terminal. Once every candidate is decided, use `freeze` with the
same arguments and `--out /path/to/new-frozen-directory`. Freezing needs an
interactive confirmation and binds the frozen decisions to the original packet.

After freezing, run `holdout_reveal.py --help` for the arguments that match the
frozen decision file, judge-only mapping, original manifest and output directory.
The reconciliation distinguishes the mechanical anchors, verification verdicts
and the owner's adjudication.

The development checkpoint `8556f98` prepared an offline packet with 39
candidates. They were awaiting owner adjudication. That preparation records a
workflow milestone, with correctness still open. Use synthetic cases in the
unit suite to verify the reader contract, freeze and reveal on a fresh checkout.
