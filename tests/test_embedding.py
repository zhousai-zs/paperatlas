import pytest

from paperatlas.errors import AtlasError


@pytest.mark.parametrize('query', [False, True])
def test_encode_checks_each_batch_item_including_prefix_and_special_tokens(fake_embedder, query):
    # The prefix takes two tokens and special tokens take another two.
    boundary = 'token ' * 508
    vectors = fake_embedder.encode(['short', boundary], query=query)
    assert vectors.shape == (2, 4)
    prefix = 'query: ' if query else 'passage: '
    assert fake_embedder.model.inputs == [prefix + 'short', prefix + boundary]
    previous_inputs = list(fake_embedder.model.inputs)
    with pytest.raises(AtlasError, match='512 tokens'):
        fake_embedder.encode(['short', boundary + 'extra'], query=query)
    assert fake_embedder.model.inputs == previous_inputs
