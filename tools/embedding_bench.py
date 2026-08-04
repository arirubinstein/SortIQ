#!/usr/bin/env python
"""Embedding + gallery bench — is metric learning a better fit than softmax?

Background: SortIQ's task is open-world recognition with a
growing class list; the closed-set softmax pair forces every case into a
known class (absorber classes, rival dilution, the training bench, and
the referee jurisdiction gate are all symptoms). This bench tests the
face-recognition alternative on the CURRENT capture setup, no hardware
changes: train an embedding, classify by nearest neighbor against a
small gallery of exemplars, reject by cosine distance.

Three tiers, judged like every SortIQ bench (live behavior over decimals):
  closed-set  the trained classes' held-out queries, kNN top-1
              (compare against the live twins' ~85-88 val)
  few-shot    the BENCHED classes are excluded from embedding training
              entirely and get a 10-exemplar gallery — can we identify
              classes the network never trained on? (headline: this is
              "new class without retraining")
  open-set    classes under the 10-image minimum act as true unknowns
              (no gallery) — sweep the cosine threshold, report unknown
              acceptance at ~95%/90% known acceptance, and wrong-identity
              acceptance among knowns (the misfile analog; must be ~0)

Run on a trainer PC (needs TensorFlow), after a dataset pull:
    python tools/embedding_bench.py [--epochs 40] [--gallery 10]
Writes embedding_bench_results.json beside this script.
"""
import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 7


def profile_dir():
    cfg = json.loads((ROOT / "config.json").read_text())
    act = cfg.get("active", {})
    return ROOT / "calibers" / act["cartridge"] / act["model"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--gallery", type=int, default=10)
    ap.add_argument("--img-size", type=int, default=160)
    ap.add_argument("--embed-dim", type=int, default=128)
    ap.add_argument("--v2", action="store_true",
                    help="ArcFace margin head + partial backbone fine-tune")
    ap.add_argument("--margin", type=float, default=0.3)
    ap.add_argument("--scale", type=float, default=30.0)
    ap.add_argument("--ft-epochs", type=int, default=15)
    args = ap.parse_args()

    import numpy as np
    import tensorflow as tf
    tf.keras.utils.set_random_seed(SEED)

    pdir = profile_dir()
    mj = json.loads((pdir / "model.json").read_text())
    crops = pdir / "data" / "stamp"
    labels = set(mj["stamp_labels"])
    benched = set(mj.get("train_disabled") or [])

    counts = {d.name: len(list(d.glob("*.png"))) + len(list(d.glob("*.jpg")))
              for d in sorted(crops.iterdir()) if d.is_dir()}
    trained = sorted(c for c in counts
                     if c in labels and c not in benched and counts[c] >= 10)
    fewshot = sorted(c for c in counts
                     if c in benched and counts[c] >= args.gallery + 2)
    unknown = sorted(c for c in counts
                     if c not in trained and c not in fewshot and counts[c] >= 3)
    print(f"trained {len(trained)} | few-shot {fewshot} | unknown {unknown}")

    rng = random.Random(SEED)
    files = {c: sorted((crops / c).glob("*.png")) + sorted((crops / c).glob("*.jpg"))
             for c in counts}
    for c in files:
        rng.shuffle(files[c])

    train_paths, train_y = [], []
    queries = []                       # (path, true_class, tier)
    gallery = []                       # (path, class)
    for i, c in enumerate(trained):
        fs = files[c]
        n_q = max(2, min(30, int(len(fs) * 0.2)))
        for f in fs[:n_q]:
            queries.append((f, c, "closed"))
        pool = fs[n_q:]
        for f in pool:
            train_paths.append(str(f))
            train_y.append(i)
        for f in pool[:args.gallery]:
            gallery.append((f, c))
    for c in fewshot:
        fs = files[c]
        for f in fs[:args.gallery]:
            gallery.append((f, c))
        for f in fs[args.gallery:args.gallery + 30]:
            queries.append((f, c, "fewshot"))
    for c in unknown:
        for f in files[c][:30]:
            queries.append((f, c, "unknown"))

    size = args.img_size

    def load(path, y):
        img = tf.io.decode_image(tf.io.read_file(path), channels=3,
                                 expand_animations=False)
        img = tf.image.resize(img, (size, size)) / 255.0
        return img, y

    ds = (tf.data.Dataset.from_tensor_slices((train_paths, train_y))
          .shuffle(len(train_paths), seed=SEED)
          .map(load, num_parallel_calls=tf.data.AUTOTUNE)
          .batch(32).prefetch(2))
    if args.v2:
        # ArcFace needs the label inside the forward pass (margin goes on
        # the true class's logit only)
        ds = ds.map(lambda x, y: ((x, y), y))

    L = tf.keras.layers
    aug = tf.keras.Sequential([
        L.RandomRotation(0.5, fill_mode="constant", fill_value=0.0),
        L.RandomTranslation(0.06, 0.06, fill_mode="constant", fill_value=0.0),
        L.RandomBrightness(0.3, value_range=(0, 1)),
        L.RandomContrast(0.15)])
    base = tf.keras.applications.MobileNetV2(
        input_shape=(size, size, 3), include_top=False, weights="imagenet")
    base.trainable = False
    inp = tf.keras.Input((size, size, 3))
    x = aug(inp)
    x = L.Rescaling(2.0, offset=-1.0)(x)
    x = base(x, training=False)
    x = L.GlobalAveragePooling2D()(x)
    x = L.Dropout(0.2)(x)
    n_cls = len(trained)

    class ArcFace(tf.keras.layers.Layer):
        """s·cos(θ+m) on the true class, s·cosθ elsewhere (insightface
        recipe, incl. the cos>cos(π−m) stability fallback). m sits in a
        variable so a warmup callback can hold it at 0 while the randomly
        initialized head finds its feet."""
        def __init__(self, n, s, m):
            super().__init__()
            self.n, self.s = n, s
            self.m = tf.Variable(m, trainable=False, dtype=tf.float32)

        def build(self, _):
            self.W = self.add_weight(name="W",
                                     shape=(args.embed_dim, self.n),
                                     initializer="glorot_uniform")

        def call(self, emb, labels):
            x = tf.nn.l2_normalize(emb, axis=1)
            W = tf.nn.l2_normalize(self.W, axis=0)
            cos = tf.clip_by_value(x @ W, -1 + 1e-7, 1 - 1e-7)
            sin = tf.sqrt(1.0 - cos * cos)
            tgt = tf.where(cos > tf.cos(3.14159265 - self.m),
                           cos * tf.cos(self.m) - sin * tf.sin(self.m),
                           cos - self.m * tf.sin(self.m))
            hot = tf.one_hot(tf.cast(labels, tf.int32), self.n)
            return self.s * (hot * tgt + (1 - hot) * cos)

    if args.v2:
        # BN'd bias-free embedding + margin head: the margin is what makes
        # cosine distance mean something at reject time (v1's open-set miss)
        e = L.Dense(args.embed_dim, use_bias=False)(x)
        emb = L.BatchNormalization(name="embedding")(e)
        lab = tf.keras.Input((), dtype="int32")
        arc = ArcFace(n_cls, args.scale, args.margin)
        model = tf.keras.Model([inp, lab], arc(emb, lab))
    else:
        emb = L.Dense(args.embed_dim, name="embedding")(x)
        out = L.Dense(n_cls, activation="softmax")(emb)
        model = tf.keras.Model(inp, out)

    def smoothed_scce(y_true, y_pred):
        # sparse CCE has no label_smoothing kwarg; smooth via one-hot
        y = tf.one_hot(tf.cast(tf.reshape(y_true, [-1]), tf.int32), n_cls)
        return tf.keras.losses.categorical_crossentropy(
            y, y_pred, from_logits=args.v2, label_smoothing=0.1)

    model.compile("adam", smoothed_scce,
                  metrics=["sparse_categorical_accuracy"])

    total = len(train_y)
    weight = {i: round(min(total / (len(trained) * train_y.count(i)), 10.0), 3)
              for i in range(len(trained))}
    cbs = ([tf.keras.callbacks.LambdaCallback(
        on_epoch_begin=lambda ep, _:
            arc.m.assign(0.0 if ep < 5 else args.margin))]
        if args.v2 else [])
    model.fit(ds, epochs=args.epochs, verbose=2, class_weight=weight,
              callbacks=cbs)

    if args.v2 and args.ft_epochs:
        # unfreeze the top third of the backbone at a gentle LR. base is
        # still called with training=False, so BN statistics stay frozen —
        # only the conv weights adapt to headstamp texture.
        base.trainable = True
        for l in base.layers[:len(base.layers) * 2 // 3]:
            l.trainable = False
        model.compile(tf.keras.optimizers.Adam(1e-4), smoothed_scce,
                      metrics=["sparse_categorical_accuracy"])
        model.fit(ds, epochs=args.ft_epochs, verbose=2, class_weight=weight)

    embedder = tf.keras.Model(inp, model.get_layer("embedding").output)

    def embed(paths):
        arr = []
        for i in range(0, len(paths), 64):
            batch = tf.stack([load(str(p), 0)[0] for p in paths[i:i + 64]])
            e = embedder(batch, training=False).numpy()
            arr.append(e)
        e = np.concatenate(arr)
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    g_vec = embed([p for p, _ in gallery])
    g_cls = [c for _, c in gallery]
    q_vec = embed([p for p, _, _ in queries])

    sims = q_vec @ g_vec.T
    top = sims.argmax(axis=1)
    results = []
    for qi, (path, true, tier) in enumerate(queries):
        t = int(top[qi])
        results.append({"true": true, "tier": tier,
                        "pred": g_cls[t], "sim": float(sims[qi][t])})

    rep = {"variant": "v2-arcface-ft" if args.v2 else "v1-softmax",
           "trained_classes": len(trained), "gallery_per_class": args.gallery,
           "epochs": args.epochs, "img_size": size}
    if args.v2:
        rep["margin"], rep["scale"] = args.margin, args.scale
        rep["ft_epochs"] = args.ft_epochs
    for tier in ("closed", "fewshot"):
        rs = [r for r in results if r["tier"] == tier]
        ok = sum(1 for r in rs if r["pred"] == r["true"])
        rep[tier] = {"n": len(rs), "top1": round(ok / max(len(rs), 1), 4)}
    per = defaultdict(lambda: [0, 0])
    for r in results:
        if r["tier"] == "fewshot":
            per[r["true"]][0] += r["pred"] == r["true"]
            per[r["true"]][1] += 1
    rep["fewshot_per_class"] = {c: f"{v[0]}/{v[1]}" for c, v in sorted(per.items())}
    cci = [r for r in results if r["true"] in ("SPEER", "NEW REP")]
    rep["cci_as_blazer"] = sum(1 for r in cci if r["pred"] == "BLAZER")
    rep["cci_n"] = len(cci)

    known = sorted(r["sim"] for r in results if r["tier"] != "unknown")
    unknown_sims = [r["sim"] for r in results if r["tier"] == "unknown"]
    rep["open_set"] = {}
    for accept in (0.95, 0.90):
        tau = known[int(len(known) * (1 - accept))]
        u_acc = sum(1 for s in unknown_sims if s >= tau)
        wrong_ok = sum(1 for r in results if r["tier"] != "unknown"
                       and r["sim"] >= tau and r["pred"] != r["true"])
        rep["open_set"][f"known_accept_{accept}"] = {
            "tau": round(float(tau), 4),
            "unknown_accepted": f"{u_acc}/{len(unknown_sims)}",
            "known_wrong_identity_accepted":
                f"{wrong_ok}/{sum(1 for r in results if r['tier'] != 'unknown')}"}

    # profile models dir, NOT tools/: everything under tools/ is part of
    # the code-sync manifest, and stray artifacts there break trainer<->
    # machine digest parity (blocks dataset pulls and code updates)
    out_path = profile_dir() / "models" / (
        "embedding_bench_results_v2.json" if args.v2
        else "embedding_bench_results.json")
    out_path.write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
    print("written:", out_path)


if __name__ == "__main__":
    main()
