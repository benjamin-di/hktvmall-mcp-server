"""Product pages and photos come from HKTVmall's own fields, checked before anyone is shown them."""

from hktvmall_mcp import links

PAGE = "main/Example-Home-Store/s/S9990010/Housewares/Housewares/Tableware/Napkins/Example-Napkins-200ct-EX1059/p/"


def test_canonical_page_from_search_and_basket_paths():
    code = "S9990010_S_EX1059"
    expected = "https://www.hktvmall.com/hktv/en/" + PAGE + code
    assert links.product_url(code, PAGE + code) == expected  # search: urlEn
    assert links.product_url(code, "/" + PAGE + code) == expected  # basket: product.url
    encoded = "main/Example-Grocer%27s-Corner/s/H9990003/Supermarket/Rice-%26-Cooking-Oil/Oil/p/H9990003_S_310000001"
    assert links.product_url("H9990003_S_310000001", encoded).endswith(encoded)


def test_an_unusable_path_falls_back_to_hktvmalls_redirect():
    code = "S9990010_S_EX1059"
    short = "https://www.hktvmall.com/hktv/en/p/" + code
    for bad in (
        None,
        "",
        PAGE + "OTHER_ID",  # another product's page
        "main/x/../../../logout/p/" + code,
        "main/x//evil.example/p/" + code,
        PAGE + code + "?x=1",
        "https://evil.example/main/x/p/" + code,
        "main/x y/p/" + code,
    ):
        assert links.product_url(code, bad) == short, bad
    assert links.product_url("not an id", PAGE) is None
    assert links.product_url(None, PAGE) is None


def test_photos_only_from_hktvmalls_cdns():
    for good in (
        "https://images.hktv-img.com/images/HKTV/10010/EX1059_main_10000001_20300101120000_01_300.jpg",
        "https://images.hktvmall.com/h9990001/100001/h9990001_20000001b_300101120000_01_300.jpg",
        "https://cdn-media.hktvmall.com/hktv-mms/HKTV/mms/uploadProductImage/0a1b/2c3d/4e5f/x_300.jpg",
        "https://cdn-mms.hktvmall.com/hktv/mms/uploadProductImage/37b/x_300.jpg",
    ):
        assert links.image_url(good) == good
    for bad in (
        None,
        "",
        "http://images.hktv-img.com/a.jpg",
        "https://hktvmall.com/a.jpg",
        "https://www.hktvmall.com/a.jpg",  # not an image CDN: the cards' CSP lists the four CDN hosts only
        "https://hktv-img.hkmpcl.com.hk/x_1200.jpg",
        "https://images.hktvmall.com.evil.example/a.jpg",
        "https://evilhktvmall.com/a.jpg",
        "https://user@images.hktvmall.com/a.jpg",
        "https://images.hktvmall.com:8443/a.jpg",
        'https://images.hktvmall.com/a.jpg"onerror=x',
        "javascript:alert(1)",
        "https://images.hktvmall.com/" + "a" * 600,
    ):
        assert links.image_url(bad) is None, bad


def test_card_photos_are_the_gallery_at_515_px_primary_first():
    gallery = [
        {"imageType": "GALLERY", "format": "product", "url": "https://cdn-media.hktvmall.com/a/b_1200.png"},
        {"imageType": "PRIMARY", "format": "product", "url": "https://images.hktv-img.com/p/main_1200.jpg"},
        {"imageType": "GALLERY", "format": "product", "url": "https://hktv-img.hkmpcl.com.hk/x_1200.jpg"},  # 404s
        {"imageType": "GALLERY", "format": "product", "url": "https://images.hktv-img.com/p/main_1200.jpg"},
    ]
    assert links.photos(gallery) == [
        "https://images.hktv-img.com/p/main_515.jpg",
        "https://cdn-media.hktvmall.com/a/b_515.png",
    ]
    many = [{"imageType": "GALLERY", "url": f"https://images.hktvmall.com/{i}_1200.webp"} for i in range(20)]
    assert len(links.photos(many)) == links.MAX_PHOTOS
    assert links.photos(None, "https://images.hktvmall.com/x_300.jpg") == ["https://images.hktvmall.com/x_300.jpg"]
    assert links.photos("junk") == []
