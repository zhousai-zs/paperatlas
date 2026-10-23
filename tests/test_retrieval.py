from paperatlas.retrieval import fuse_pages


def test_different_chunks_on_one_page_share_cross_channel_support():
    pages = {1: ('A', 1), 2: ('B', 1), 3: ('A', 1), 4: ('C', 1)}
    fused = fuse_pages([[2, 1], [4, 3]], pages)
    assert pages[fused[0][0]] == ('A', 1)
    assert len(fused) == 3
    assert len({pages[identity] for identity, _ in fused}) == 3
    # The anchor is an actual hit, with deterministic tie breaking.
    assert fused[0][0] == 1


def test_duplicate_chunks_do_not_push_later_pages_down_the_channel_ranking():
    pages = {1: ('A', 1), 2: ('A', 1), 3: ('B', 2)}
    with_duplicates = fuse_pages([[1, 2, 3]], pages)
    without_duplicates = fuse_pages([[1, 3]], pages)
    assert with_duplicates == without_duplicates
