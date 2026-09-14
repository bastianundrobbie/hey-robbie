# models/

Put the wakeword model here as `hey_robbie.onnx` (openWakeWord classifier;
the server loads it from this directory, override with `ROBBIE_WAKE_MODEL`).

The model is **not** part of this repository. Two ways to get one:

- **A — download:** a pre-trained "hey robbie" model from a public collection
  of openWakeWord models. <PLACEHOLDER: source link>
- **B — train your own wake word** with the openWakeWord training notebook
  (recommended if you want a different name than Robbie). The key inside the
  model must match `wakeword = "hey_robbie"` in `server/config/config.toml`
  and `WAKE_KEY` in `server/duplex.py`.

`*.onnx` files in this directory are git-ignored.
