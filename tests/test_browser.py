"""Drives the sharing page in a real browser: the link row's copy, open and QR code controls."""

import pytest
from playwright.sync_api import expect, sync_playwright

from sharing_manager import runner

pytestmark = pytest.mark.browser

FILES = "https://files-test.example.test/"
LONG_DOMAIN = "6be2dce11f5f77dec75349013c1384f4.d614ca37b19c752b08032764976c2d97.us1.personal-imbue.com"


@pytest.fixture
def browser():
    with sync_playwright() as p:
        launched = p.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion")
        yield launched
        launched.close()


@pytest.fixture
def open_page(browser, serve):
    contexts = []

    def open_at(target="files", **serve_options):
        context = browser.new_context(viewport={"width": 1100, "height": 900})
        contexts.append(context)
        page = context.new_page()
        page.goto(serve(**serve_options) + "?target=" + target)
        page.wait_for_selector("#permissions:not([hidden])")
        return page

    yield open_at
    for context in contexts:
        context.close()


def decoded(page, selector="#qr-code"):
    """What a phone camera would read from the drawn code."""
    zxingcpp = pytest.importorskip("zxingcpp")
    pillow = pytest.importorskip("PIL.Image")
    import io

    image = pillow.open(io.BytesIO(page.locator(selector).screenshot()))
    found = zxingcpp.read_barcodes(image)
    return [barcode.text for barcode in found]


def test_the_link_row_offers_copy_then_open_then_qr_code(open_page):
    page = open_page()
    order = page.eval_on_selector_all(".copy-row > *", "elements => elements.map(e => e.id)")
    assert order == ["share-link", "copy-link", "open-link", "show-qr"]
    assert page.input_value("#share-link") == FILES
    assert page.is_enabled("#copy-link") and page.is_enabled("#show-qr")
    assert page.get_attribute("#open-link", "title") == "Open in browser"
    assert page.get_attribute("#show-qr", "title") == "Show QR code"


def test_open_goes_to_the_link_in_a_new_tab(open_page):
    page = open_page()
    assert page.get_attribute("#open-link", "href") == FILES
    assert page.get_attribute("#open-link", "target") == "_blank"
    assert "noopener" in page.get_attribute("#open-link", "rel")
    page.context.route("https://files-test.example.test/**", lambda route: route.fulfill(body="opened", content_type="text/plain"))
    with page.context.expect_page() as popup:
        page.click("#open-link")
    assert popup.value.url == FILES
    assert page.url.startswith("http://127.0.0.1")


def test_open_follows_the_selected_app(open_page):
    page = open_page()
    page.click("[data-share-target=notes]")
    assert page.input_value("#share-link") == "https://notes-test.example.test/"
    assert page.get_attribute("#open-link", "href") == "https://notes-test.example.test/"


def test_the_qr_code_opens_in_a_dialog_and_closes_three_ways(open_page):
    page = open_page()
    page.click("#show-qr")
    expect(page.locator("#qr-dialog")).to_be_visible()
    assert page.inner_text("#qr-title") == "QR code for Files"
    assert page.inner_text("#qr-link") == FILES
    assert page.locator("#qr-code svg path").count() == 1
    page.click("#close-qr")
    expect(page.locator("#qr-dialog")).to_be_hidden()
    page.click("#show-qr")
    page.keyboard.press("Escape")
    expect(page.locator("#qr-dialog")).to_be_hidden()
    page.click("#show-qr")
    page.mouse.click(5, 5)
    expect(page.locator("#qr-dialog")).to_be_hidden()
    page.click("#show-qr")
    page.click("#qr-code")
    expect(page.locator("#qr-dialog")).to_be_visible()


def test_the_qr_code_scans_back_to_the_link(open_page):
    page = open_page()
    page.click("#show-qr")
    assert decoded(page) == [FILES]


def test_a_realistic_long_link_still_scans(open_page):
    page = open_page(domain=LONG_DOMAIN)
    link = f"https://files-test.{LONG_DOMAIN}/"
    assert page.input_value("#share-link") == link
    page.click("#show-qr")
    assert decoded(page) == [link]


def test_the_qr_code_follows_the_whole_workspace_target(open_page):
    page = open_page(target="workspace")
    page.click("#show-qr")
    assert page.inner_text("#qr-title") == "QR code for the whole workspace"
    assert decoded(page) == ["https://shell-test.example.test/"]


def test_every_link_control_is_off_while_sharing_is_off(open_page):
    page = open_page(state="off")
    assert page.is_disabled("#copy-link") and page.is_disabled("#show-qr")
    assert page.get_attribute("#open-link", "href") is None
    assert page.get_attribute("#open-link", "aria-disabled") == "true"


def test_every_link_control_is_off_for_an_app_without_a_link(open_page):
    page = open_page(target="unlabelled")
    assert page.input_value("#share-link") == "Link not available yet"
    assert page.is_disabled("#copy-link") and page.is_disabled("#show-qr")
    assert page.get_attribute("#open-link", "href") is None


def test_the_link_controls_stay_on_after_a_permission_change(open_page):
    page = open_page()
    page.fill("#grant-value", "person@example.org")
    page.click("#add-grant")
    page.wait_for_selector("[data-grant-value='person@example.org']")
    assert page.is_enabled("#copy-link") and page.is_enabled("#show-qr")
    assert page.get_attribute("#open-link", "href") == FILES
    page.click("#show-qr")
    expect(page.locator("#qr-dialog")).to_be_visible()


def test_the_visiting_agents_section_is_hidden_where_there_is_no_support(serve, browser, monkeypatch):
    base = serve()
    monkeypatch.setattr(runner, "IS_SANDBOX", False)
    monkeypatch.setattr(runner, "agent_access_command", lambda: None)
    page = browser.new_page()
    page.goto(base + "?target=files")
    page.wait_for_selector("#permissions:not([hidden])")
    expect(page.locator("#agent-section")).to_be_hidden()
    page.close()


def test_the_page_names_the_machine_it_manages(serve, browser, monkeypatch):
    base = serve()
    monkeypatch.setattr(runner, "MACHINE_NAME", "worker-086")
    page = browser.new_page()
    page.goto(base)
    page.wait_for_selector("#permissions:not([hidden])")
    assert "worker-086" in page.inner_text("#page-subtitle")
    page.close()
