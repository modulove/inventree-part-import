import re
from typing import Any

from bs4 import BeautifulSoup
from error_helper import warning
from requests.compat import quote

from ..localization import get_country, get_language
from .base import ApiPart, ScrapeSupplier, SupplierSupportLevel, money2float

BASE_URL = "https://www.reichelt.com"
PRODUCT_PAGE = "shop/product"
SEARCH_PAGE = "shop/search"


class Reichelt(ScrapeSupplier):
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    def setup(
        self,
        *,
        language: str,
        location: str,
        scraping: str,
        interactive_part_matches: int,
        browser_cookies: str = "",
        **kwargs: Any,
    ):
        if not scraping:
            self.load_error("scraping is disabled")

        if not get_country(location):
            self.load_error(f"unsupported location '{location}'")

        if not get_language(language):
            self.load_error(f"invalid language code '{language}'")

        self.language = language
        self.location = location
        self.localized_url = f"{BASE_URL}/{self.location.lower()}/{self.language.lower()}"

        if browser_cookies:
            self.cookies_from_browser(browser_cookies, "reichelt.com")

        self.max_results = interactive_part_matches

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        search_term = search_term.strip()

        # direct lookup by reichelt product id ("387191" or "P387191")
        if product_id_match := PRODUCT_ID_REGEX.fullmatch(search_term):
            if api_part := self.get_product(product_id_match.group(1)):
                return [api_part], 1
            return [], 0

        search_url = f"{self.localized_url}/{SEARCH_PAGE}/{quote(search_term, safe='')}"
        if not (result := self.scrape(search_url)):
            return [], 0

        search_soup = BeautifulSoup(result.content, "html.parser")
        product_ids: list[str] = []
        for url_tag in search_soup.select('div.al_gallery_article a[itemprop="url"]'):
            if not isinstance(product_url := url_tag.get("href"), str):
                continue
            if (id_match := PRODUCT_URL_ID_REGEX.match(product_url)) and (
                id_match.group(1) not in product_ids
            ):
                product_ids.append(id_match.group(1))

        api_parts = [
            api_part
            for product_id in product_ids[: self.max_results]
            if (api_part := self.get_product(product_id))
        ]

        exact_matches = [
            api_part
            for api_part in api_parts
            if api_part.SKU.lower() == search_term.lower()
            or api_part.MPN.lower() == search_term.lower()
        ]
        if len(exact_matches) == 1:
            return [exact_matches[0]], 1

        # the reichelt search is fuzzy ("C1525" also finds "SAC15250"),
        # so only keep products which contain the search term as a whole word
        whole_word_regex = re.compile(
            rf"(?<![\w-]){re.escape(search_term)}(?![\w-])", re.IGNORECASE
        )
        api_parts = [
            api_part
            for api_part in api_parts
            if whole_word_regex.search(f"{api_part.SKU} {api_part.MPN} {api_part.description}")
        ]

        n_results = len(product_ids)
        return api_parts, n_results if n_results > self.max_results else len(api_parts)

    def get_product(self, product_id: str) -> ApiPart | None:
        # reichelt redirects this to the canonical product url (".../<slug>-<id>")
        if not (product_page := self.scrape(f"{self.localized_url}/{PRODUCT_PAGE}/-{product_id}")):
            return None
        product_page_soup = BeautifulSoup(product_page.content, "html.parser")
        return self.get_api_part(product_page_soup, product_id, product_page.url)

    def get_api_part(self, soup: BeautifulSoup, product_id: str, url: str):
        assert (name_tag := soup.select_one('h1[itemprop="name"]'))
        description = name_tag.text.strip()

        # article number as shown in the shop (e.g. "XIAO RP2350"), falling back to the id
        sku = f"P{product_id}"
        if sku_tag := soup.select_one('[itemprop="sku"]'):
            sku = sku_tag.text.strip() or sku

        mpn = sku
        if mpn_tag := soup.select_one('[itemprop="mpn"]'):
            mpn = mpn_tag.text.strip() or mpn

        image_url = None
        for image_tag in soup.select('a[href*="/bilder/web/xxl"], img[itemprop="image"]'):
            image_src = image_tag.get("href") or image_tag.get("src")
            if isinstance(image_src, str):
                image_url = IMAGE_URL_REGEX.sub(IMAGE_URL_SUB, image_src)
                break

        datasheet_url = None
        for link_tag in soup.select("a[href]"):
            if isinstance(href := link_tag["href"], str) and DATASHEET_URL_REGEX.search(href):
                datasheet_url = href
                break

        availability = 0
        if availability_tag := soup.select_one("a.availability"):
            status = next((c for c in availability_tag["class"] if c.startswith("status_")), None)
            if status:
                if status not in AVAILABILITY_MAP:
                    warning(f"unknown reichelt availability '{status}' ({url})")
                availability = AVAILABILITY_MAP.get(status, 0)

        category_path = [
            span.text.strip()
            for span in soup.select('ol#breadcrumb li a span[itemprop="name"]')[1:]
        ]

        parameters: dict[str, str] = {}
        for attribute_list in soup.select("ul.articleAttribute"):
            items = [li.text.strip() for li in attribute_list.find_all("li", recursive=False)]
            for name, value in zip(items[::2], items[1::2]):
                if name and value and value != "-":
                    parameters.setdefault(name, value)

        manufacturer = parameters.get("Manufacturer")
        if not manufacturer and (brand_tag := soup.select_one('[itemprop="brand"]')):
            manufacturer = brand_tag.text.strip()
        if not manufacturer:
            manufacturer = "Reichelt"

        assert (meta_price := soup.select_one('meta[itemprop="price"]'))
        assert isinstance(meta_price_content := meta_price["content"], str)
        price_breaks: dict[int | float, float] = {1: money2float(meta_price_content)}
        for discount_tag in soup.select("div.discountValue ul li p#productPrice")[1:]:
            assert discount_tag.parent and (quantity_tag := discount_tag.parent.select_one("span"))
            quantity_str = PRICE_BREAK_QUANTITIY_REGEX.sub("", quantity_tag.text).strip()
            assert " " not in quantity_str
            price_breaks[float(quantity_str)] = money2float(discount_tag.text)

        currency = "EUR"
        if meta_currency := soup.select_one('meta[itemprop="priceCurrency"]'):
            assert isinstance(currency := meta_currency["content"], str)

        return ApiPart(
            description=description,
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=url,
            SKU=sku,
            manufacturer=manufacturer,
            manufacturer_link="",
            MPN=mpn,
            quantity_available=availability,
            packaging="",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )


IMAGE_URL_REGEX = re.compile(r"/resize/[^/]+/([^?]+)\?.*")
IMAGE_URL_SUB = r"/images/\g<1>"
DATASHEET_URL_REGEX = re.compile(r"/documents/datenblatt/.*\.pdf", re.IGNORECASE)
PRODUCT_ID_REGEX = re.compile(r"^[pP]?(\d{4,8})$")
PRODUCT_URL_ID_REGEX = re.compile(r"^.*/shop/product/[^?#]*-(\d+)(?:[?#].*)?$")
PRICE_BREAK_QUANTITIY_REGEX = re.compile(r"[^0-9 ]")

# True -> available, 0 -> not available
AVAILABILITY_MAP = {
    "status_1": True,
    "status_2": 0,
    "status_3": True,
    "status_4": True,
    "status_5": 0,
    "status_6": True,
    "status_7": True,
    "status_8": 0,
    "status_16": True,
}
