"""
Which regions a headline is about, from the places it names.

Shared topic feeds (Mongabay, the Guardian's environment feeds, good-news
outlets) cover the whole world, so without this every region's topic view
shows the same stories. A headline that names a country, its people, or a
major city or state is shown in that place's region (feed_service.
build_topic_views); one that names no place stays in every region.

A plain list, not a model: headlines name places plainly ("Sierra Leone's
Mahera Coast", "Brazilian Amazon states"), and a list is predictable. Names
that are often something else are left out: Georgia (a US state and a
country), Jordan, Chad, Turkey (turkey vultures), Victoria, Amazon (the
company), English and Polish.
Antarctica, the Arctic and the oceans belong to no region.
"""

import re
from functools import lru_cache
from typing import FrozenSet

# Matched as written: as lowercase words they're ordinary English ("us")
_EXACT = {"US": "americas", "U.S.": "americas", "USA": "americas", "UK": "europe", "U.K.": "europe",
          "EU": "europe", "UAE": "asia", "DRC": "africa", "PNG": "oceania", "NSW": "oceania"}

# Comma-separated; a name may have several words. Each name is listed once.
_PLACES = {
    "africa": """
        Africa, African, Algeria, Algerian, Angola, Angolan, Benin, Botswana, Burkina Faso, Burundi,
        Cameroon, Cameroonian, Cape Verde, Central African Republic, Comoros, Congo, Congolese, Djibouti,
        Egypt, Egyptian, Equatorial Guinea, Eritrea, Eritrean, Eswatini, Swaziland, Ethiopia, Ethiopian,
        Gabon, Gambia, Ghana, Ghanaian, Guinea, Guinea-Bissau, Ivory Coast, Côte d'Ivoire, Ivorian, Kenya,
        Kenyan, Lesotho, Liberia, Liberian, Libya, Libyan, Madagascar, Malagasy, Malawi, Malawian, Mali,
        Malian, Mauritania, Mauritius, Morocco, Moroccan, Mozambique, Mozambican, Namibia, Namibian, Niger,
        Nigeria, Nigerian, Rwanda, Rwandan, Senegal, Senegalese, Seychelles, Sierra Leone, Somalia, Somali,
        Somaliland, South Africa, South African, South Sudan, Sudan, Sudanese, Tanzania, Tanzanian, Togo,
        Tunisia, Tunisian, Uganda, Ugandan, Zambia, Zambian, Zimbabwe, Zimbabwean, Western Sahara, Sahara,
        Sahel, Darfur, Zanzibar, Serengeti, Kilimanjaro, Okavango, Kruger, Chadian, Lagos, Abuja, Nairobi,
        Mombasa, Cairo, Johannesburg, Cape Town, Durban, Pretoria, Accra, Kampala, Addis Ababa, Kinshasa,
        Dakar, Khartoum, Casablanca, Tunis, Algiers, Harare, Lusaka, Kigali, Dar es Salaam
    """,
    "asia": """
        Asia, Asian, Middle East, Afghanistan, Afghan, Armenia, Armenian, Azerbaijan, Bahrain, Bangladesh,
        Bangladeshi, Bhutan, Brunei, Cambodia, Cambodian, China, Chinese, Hong Kong, Macau, Taiwan,
        Taiwanese, India, Indian, Indonesia, Indonesian, Iran, Iranian, Iraq, Iraqi, Israel, Israeli,
        Palestine, Palestinian, Gaza, West Bank, Japan, Japanese, Jordanian, Kazakhstan, Kazakh, Kuwait,
        Kuwaiti, Kyrgyzstan, Laos, Lebanon, Lebanese, Malaysia, Malaysian, Maldives, Mongolia, Mongolian,
        Myanmar, Burma, Burmese, Nepal, Nepali, Nepalese, North Korea, South Korea, Korea, Korean, Oman,
        Pakistan, Pakistani, Philippines, Philippine, Filipino, Qatar, Qatari, Saudi Arabia, Saudi,
        Singapore, Singaporean, Sri Lanka, Sri Lankan, Syria, Syrian, Tajikistan, Thailand, Thai,
        Timor-Leste, Turkmenistan, United Arab Emirates, Emirati, Uzbekistan, Uzbek, Vietnam, Vietnamese,
        Yemen, Yemeni, Himalaya, Himalayas, Himalayan, Tibet, Tibetan, Kashmir, Borneo, Sumatra, Bali,
        Mekong, Beijing, Shanghai, Shenzhen, Tokyo, Osaka, Seoul, Delhi, New Delhi, Mumbai, Bengaluru,
        Bangalore, Chennai, Kolkata, Hyderabad, Karachi, Lahore, Islamabad, Dhaka, Kathmandu, Colombo,
        Bangkok, Hanoi, Ho Chi Minh City, Jakarta, Manila, Kuala Lumpur, Dubai, Abu Dhabi, Doha, Riyadh,
        Tehran, Baghdad, Jerusalem, Tel Aviv, Beirut, Damascus, Kabul, Taipei, Ladakh, Kerala, Maharashtra,
        Tamil Nadu, Assam, Gujarat, Karnataka, Uttarakhand
    """,
    "europe": """
        Europe, European, Albania, Albanian, Andorra, Austria, Austrian, Belarus, Belgium, Belgian, Bosnia,
        Bulgaria, Bulgarian, Croatia, Croatian, Cyprus, Czech, Czechia, Denmark, Danish, Estonia, Estonian,
        Finland, Finnish, France, French, Germany, German, Greece, Greek, Hungary, Hungarian, Iceland,
        Icelandic, Ireland, Irish, Italy, Italian, Kosovo, Latvia, Latvian, Liechtenstein, Lithuania,
        Lithuanian, Luxembourg, Malta, Moldova, Monaco, Montenegro, Netherlands, Holland, Dutch, North
        Macedonia, Norway, Norwegian, Poland, Portugal, Portuguese, Romania, Romanian, Russia, Russian, San
        Marino, Serbia, Serbian, Slovakia, Slovak, Slovenia, Slovenian, Spain, Spanish, Sweden, Swedish,
        Switzerland, Swiss, Ukraine, Ukrainian, Britain, British, Great Britain, United Kingdom, England,
        Scotland, Scottish, Wales, Welsh, Northern Ireland, Türkiye, Turkish, Balkans, Balkan, Baltic,
        Scandinavia, Scandinavian, Alps, Alpine, London, Paris, Berlin, Madrid, Barcelona, Rome, Milan,
        Venice, Vienna, Amsterdam, Brussels, Lisbon, Dublin, Edinburgh, Manchester, Liverpool, Glasgow,
        Athens, Warsaw, Prague, Budapest, Bucharest, Sofia, Kyiv, Kiev, Moscow, Stockholm, Oslo, Copenhagen,
        Helsinki, Munich, Hamburg, Frankfurt, Zurich, Geneva, Istanbul, Ankara, Kharkiv, Odesa, Belgrade,
        Zagreb
    """,
    "americas": """
        America, American, Americas, North America, South America, Latin America, Latin American, Central
        America, Caribbean, United States, Canada, Canadian, Mexico, Mexican, Guatemala, Belize, Honduras,
        El Salvador, Nicaragua, Costa Rica, Panama, Cuba, Cuban, Haiti, Haitian, Dominican Republic,
        Jamaica, Jamaican, Puerto Rico, Bahamas, Barbados, Trinidad, Colombia, Colombian, Venezuela,
        Venezuelan, Ecuador, Ecuadorian, Peru, Peruvian, Bolivia, Bolivian, Brazil, Brazilian, Paraguay,
        Uruguay, Argentina, Argentine, Argentinian, Chile, Chilean, Guyana, Suriname, Greenland, Patagonia,
        Andes, Galapagos, Galápagos, Yellowstone, New England, New Jersey, Alabama, Alaska, Arizona,
        Arkansas, California, Colorado, Connecticut, Delaware, Florida, Hawaii, Idaho, Illinois, Indiana,
        Iowa, Kansas, Kentucky, Louisiana, Maine, Maryland, Massachusetts, Michigan, Minnesota, Mississippi,
        Missouri, Montana, Nebraska, Nevada, New Hampshire, New Mexico, New York, North Carolina, North
        Dakota, Ohio, Oklahoma, Oregon, Pennsylvania, Rhode Island, South Carolina, South Dakota, Tennessee,
        Texas, Utah, Vermont, Virginia, Washington, West Virginia, Wisconsin, Wyoming, Ontario, Quebec,
        British Columbia, Alberta, Manitoba, Saskatchewan, Nova Scotia, Newfoundland, Los Angeles, Chicago,
        Houston, Miami, San Francisco, Seattle, Boston, Toronto, Vancouver, Montreal, Mexico City, São
        Paulo, Sao Paulo, Rio de Janeiro, Buenos Aires, Lima, Bogotá, Bogota, Santiago, Caracas, Havana
    """,
    "oceania": """
        Oceania, Australia, Australian, Aotearoa, New Zealand, Fiji, Fijian, Papua New Guinea, Samoa,
        Samoan, Tonga, Tongan, Vanuatu, Solomon Islands, Kiribati, Tuvalu, Nauru, Palau, Micronesia,
        Marshall Islands, New Caledonia, Tahiti, Cook Islands, Pacific Islands, Great Barrier Reef,
        Tasmania, Queensland, New South Wales, Māori, Maori, Sydney, Melbourne, Brisbane, Perth, Adelaide,
        Canberra, Auckland, Wellington, Christchurch
    """,
}


def _terms():
    """{name: region}. A name can wrap onto the next line: its whitespace counts as one space."""
    return {" ".join(name.split()): region for region, text in _PLACES.items() for name in text.split(",") if name.strip()}


_TERMS = _terms()
# Longest names first, so "New South Wales" wins over "Wales" and "Papua New Guinea" over "Guinea"
_PATTERN = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(t) for t in sorted(_TERMS, key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)
_EXACT_PATTERN = re.compile(r"(?<![\w.])(" + "|".join(re.escape(t) for t in _EXACT) + r")(?![\w])")
_BY_LOWER = {t.lower(): r for t, r in _TERMS.items()}


def item_regions(item: dict) -> FrozenSet[str]:
    """A headline's regions; if its title names no place, those its feed text names in its first 300 characters."""
    return regions_in(item["title"]) or regions_in((item.get("feed_text") or "")[:300])


@lru_cache(maxsize=20000)
def regions_in(title: str) -> FrozenSet[str]:
    """The regions whose places a headline names (empty if none)."""
    found = {_BY_LOWER[m.group(1).lower()] for m in _PATTERN.finditer(title)}
    found |= {_EXACT[m.group(1)] for m in _EXACT_PATTERN.finditer(title)}
    return frozenset(found)
