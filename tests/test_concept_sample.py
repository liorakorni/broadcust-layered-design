"""Evidence taken from the concept: which overlay samples can be trusted."""

from lib import concept_sample


# --- overlay samples ---------------------------------------------------------


def _concept_with_black_panel():
    """Photograph-like noise on the right, a flat black panel on the left."""
    import random

    from PIL import Image

    rng = random.Random(3)
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    pixels = image.load()
    for y in range(400):
        for x in range(200, 400):
            v = rng.randint(40, 200)
            pixels[x, y] = (v, v // 2, v // 3)
    return image


def _png(image):
    import io

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _scene(samples):
    return {"canvas": {"width": 400, "height": 400}, "overlaySamples": samples}


def _sample(kind, x, y):
    return {"kind": kind, "x": x, "y": y, "width": 60, "height": 60}


def test_a_transparent_sample_on_flat_black_is_no_evidence_and_is_dropped():
    concept = _png(_concept_with_black_panel())
    scene = _scene([_sample("transparent", 20, 20), _sample("transparent", 260, 100),
                    _sample("opaque", 20, 300)])
    kept, dropped = concept_sample.drop_uninformative_samples(concept, scene)
    assert [(s["kind"], s["x"]) for s in kept] == [("transparent", 260), ("opaque", 20)]
    assert [d["x"] for d in dropped] == [20]
    assert dropped[0]["mean"] < concept_sample.FLAT_BLACK_MEAN


def test_opaque_samples_are_never_dropped_for_being_black():
    concept = _png(_concept_with_black_panel())
    scene = _scene([_sample("opaque", 20, 20), _sample("transparent", 260, 100)])
    kept, dropped = concept_sample.drop_uninformative_samples(concept, scene)
    assert len(kept) == 2 and dropped == []


def test_samples_are_kept_when_dropping_would_leave_nothing_to_verify():
    concept = _png(_concept_with_black_panel())
    scene = _scene([_sample("transparent", 20, 20), _sample("transparent", 20, 200)])
    kept, dropped = concept_sample.drop_uninformative_samples(concept, scene)
    assert kept == scene["overlaySamples"] and dropped == []


def test_a_dark_but_photographic_region_is_kept():
    from PIL import Image

    image = Image.new("RGB", (400, 400), (30, 28, 26))
    kept, dropped = concept_sample.drop_uninformative_samples(
        _png(image), _scene([_sample("transparent", 100, 100)]))
    assert len(kept) == 1 and dropped == []
