"""Supported countries and their states/provinces/regions for signup.

A fixed, curated list (not a general-purpose geo database) covering the
countries this app supports. Both fields are stored as plain display text on
Profile (e.g. country="India", state="Kerala") rather than codes, so admin
screens and the leaderboard need no extra lookup to be readable.
"""

STATES_BY_COUNTRY = {
    # Assam, Andhra Pradesh, Odisha, Nagaland, Sikkim and Telangana are
    # deliberately left out: residents can't claim prizes (Terms section 1).
    "India": [
        "Arunachal Pradesh",
        "Bihar",
        "Chhattisgarh",
        "Goa",
        "Gujarat",
        "Haryana",
        "Himachal Pradesh",
        "Jharkhand",
        "Karnataka",
        "Kerala",
        "Madhya Pradesh",
        "Maharashtra",
        "Manipur",
        "Meghalaya",
        "Mizoram",
        "Punjab",
        "Rajasthan",
        "Tamil Nadu",
        "Tripura",
        "Uttar Pradesh",
        "Uttarakhand",
        "West Bengal",
        "Andaman and Nicobar Islands",
        "Chandigarh",
        "Dadra and Nagar Haveli and Daman and Diu",
        "Delhi (National Capital Territory)",
        "Jammu and Kashmir",
        "Ladakh",
        "Lakshadweep",
        "Puducherry",
    ],
    "Bahrain": [
        "Capital Governorate",
        "Muharraq Governorate",
        "Northern Governorate",
        "Southern Governorate",
    ],
    "Kuwait": [
        "Al Asimah (Capital)",
        "Hawalli",
        "Farwaniya",
        "Mubarak Al-Kabeer",
        "Ahmadi",
        "Jahra",
    ],
    "Oman": [
        "Muscat",
        "Dhofar",
        "Musandam",
        "Al Buraimi",
        "Al Dakhiliyah",
        "Al Batinah North",
        "Al Batinah South",
        "Al Sharqiyah North",
        "Al Sharqiyah South",
        "Al Dhahirah",
        "Al Wusta",
    ],
    "Qatar": [
        "Doha",
        "Al Rayyan",
        "Al Wakrah",
        "Al Khor",
        "Umm Salal",
        "Al Daayen",
        "Al Shamal",
        "Al Shahaniya",
    ],
    "Saudi Arabia": [
        "Riyadh",
        "Makkah",
        "Madinah",
        "Eastern Province",
        "Asir",
        "Tabuk",
        "Qassim",
        "Hail",
        "Northern Borders",
        "Jazan",
        "Najran",
        "Al Bahah",
        "Al Jouf",
    ],
    "United Arab Emirates (UAE)": [
        "Abu Dhabi",
        "Dubai",
        "Sharjah",
        "Ajman",
        "Umm Al Quwain",
        "Ras Al Khaimah",
        "Fujairah",
    ],
    "United Kingdom (UK)": [
        "England",
        "Scotland",
        "Wales",
        "Northern Ireland",
    ],
    "United States (USA)": [
        "Alabama",
        "Alaska",
        "Arizona",
        "Arkansas",
        "California",
        "Colorado",
        "Connecticut",
        "Delaware",
        "Florida",
        "Georgia",
        "Hawaii",
        "Idaho",
        "Illinois",
        "Indiana",
        "Iowa",
        "Kansas",
        "Kentucky",
        "Louisiana",
        "Maine",
        "Maryland",
        "Massachusetts",
        "Michigan",
        "Minnesota",
        "Mississippi",
        "Missouri",
        "Montana",
        "Nebraska",
        "Nevada",
        "New Hampshire",
        "New Jersey",
        "New Mexico",
        "New York",
        "North Carolina",
        "North Dakota",
        "Ohio",
        "Oklahoma",
        "Oregon",
        "Pennsylvania",
        "Rhode Island",
        "South Carolina",
        "South Dakota",
        "Tennessee",
        "Texas",
        "Utah",
        "Vermont",
        "Virginia",
        "Washington",
        "West Virginia",
        "Wisconsin",
        "Wyoming",
        "District of Columbia (Washington, DC)",
    ],
}

COUNTRIES = list(STATES_BY_COUNTRY.keys())
COUNTRY_CHOICES = [(name, name) for name in COUNTRIES]

# Every state across every country, for the state field's ChoiceField.choices
# (which must accept whichever state a valid POST names, whatever the
# selected country). The cross-field check that the state actually belongs
# to the chosen country happens separately, in RegistrationForm.clean().
_ALL_STATES = sorted({state for states in STATES_BY_COUNTRY.values() for state in states})
STATE_CHOICES = [(name, name) for name in _ALL_STATES]


def states_for(country):
    """The valid state list for `country`, or [] if it isn't supported."""
    return STATES_BY_COUNTRY.get(country, [])
