"""
categories.py — product-category classifier for Indian qcomm + e-commerce.

Simple, fast keyword rules tuned to how products are actually named on Blinkit/
Instamart/Zepto/Amazon/Flipkart. Order matters: more specific categories are
checked first so "face cream" lands in Personal Care, not Dairy.

Used to group everything the system sees: crawled catalog rows (price_obs) and
every search result stored for future analysis.
"""
from __future__ import annotations

# Ordered: first match wins. Keep specific phrases before generic tokens.
RULES = [
    ("Baby Care", ["diaper", "pampers", "baby lotion", "baby soap", "baby food",
                   "cerelac", "baby oil", "feeding bottle"]),
    ("Pet Care", ["dog food", "cat food", "pedigree", "whiskas", "pet ", "kitten", "puppy"]),
    ("Electronics & Appliances", [
        "iphone", "samsung galaxy", "oneplus", "redmi", "phone", "smartphone",
        "laptop", "macbook", "tablet", "ipad", "headphone", "earbud", "airpod",
        "bluetooth speaker", "speaker", "charger", "power bank", "smartwatch",
        "fitness band", "television", " led tv", "monitor", "camera", "mouse",
        "keyboard", "ssd", "hard disk", "pendrive", "trimmer", "mixer grinder",
        "geyser", "iron ", "fan ", "air fryer", "refrigerator", "washing machine"]),
    ("Fashion & Accessories", [
        "tshirt", "t-shirt", " shirt", "jeans", "trouser", "kurti", "kurta",
        "saree", "dress", "jacket", "hoodie", "shoe", "sneaker", "sandal",
        "chappal", "slipper", "handbag", "wallet", "belt ", "sunglass"]),
    ("Personal Care", [
        "soap", "shampoo", "conditioner", "toothpaste", "toothbrush", "mouthwash",
        "deodorant", " deo ", "perfume", "face wash", "face cream", "sunscreen",
        "moisturiser", "moisturizer", "body lotion", "hair oil", "hair gel",
        "razor", "shaving", "sanitizer", "lipstick", "kajal", "eyeliner",
        "nail paint", "powder", "diaper rash"]),
    ("Home & Cleaning", [
        "detergent", "surf excel", "rin ", "ariel", "vim ", "dishwash", "dish wash",
        "phenyl", "harpic", "toilet cleaner", "floor cleaner", "lizol", "domex",
        "mop ", "broom", "garbage bag", "dustbin", "tissue", "napkin", "foil",
        "cling wrap", "matchbox", "candle", "room freshener", "naphthalene"]),
    ("Pharmacy & Wellness", [
        "dolo", "paracetamol", "crocin", "vicks", "balm", "bandaid", "band-aid",
        "antiseptic", "vitamin", "supplement", "protein powder", "whey",
        "chyawanprash", "hand sanitizer", "thermometer", "ors "]),
    ("Dairy & Eggs", [
        "milk", "dahi", "curd", "yogurt", "paneer", "cheese", "butter", "ghee",
        "lassi", "chaas", "buttermilk", "egg", "tofu", "ice cream", "kulfi",
        "flavoured milk", "amul ", "mother dairy"]),
    ("Beverages", [
        "juice", "cola", "pepsi", "coke", "soda", " green tea", " tea bag",
        "coffee", "nescafe", "bru ", "energy drink", "bournvita", "horlicks",
        "complan", "mineral water", "packaged water", "jaljeera", "nimbu pani",
        "thums up", "sprite", "fanta", "mojito"]),
    ("Snacks & Branded Foods", [
        "chips", "lays", "kurkure", "bingo", "namkeen", "bhujia", "sev ",
        "biscuit", "cookie", "parle-g", "oreo", "bourbon", "good day", "marie",
        "popcorn", "nachos", "tortilla", "chocolate", "cadbury", "kitkat",
        "snickers", "dairy milk silk" + "", "cake", "pastry", "rusk", "khari",
        "instant noodles", "maggi", "yippee", "knorr soup", "soup", "pasta",
        "spaghetti", "corn flakes", "muesli", "oats", "granola", "spread",
        "peanut butter", "honey", "jam", "ketchup", "mayonnaise", "sauce",
        "pickles", "achar"]),
    ("Staples, Atta, Rice, Oil", [
        "atta", "rice", "basmati", "dal ", "toor", "moong", "chana", "rajma",
        "chhole", "oil", "sunflower", "mustard oil", "groundnut", "ghee substitute",
        "sugar", "salt", "turmeric", "haldi", "mirchi", "chilli powder",
        "coriander powder", "jeera", "cumin", "garam masala", "poha", "sooji",
        "rava", "maida", "besan", "sabudana", "dry fruit", "almond", "cashew",
        "raisin", "makhana", "peanut"]),
    ("Fruits & Vegetables", [
        "apple", "banana", "mango", "orange", "grapes", "papaya", "pomegranate",
        "watermelon", "onion", "potato", "tomato", "carrot", "capsicum",
        "cucumber", "spinach", "palak", "methi", "coriander leaves", "mint",
        "pudina", "lemon", "ginger", "garlic", "cauliflower", "cabbage",
        "brinjal", "okra", "bhindi", "beans", "peas", "green chilli", "broccoli"]),
]

DEFAULT = "Other"


def categorize(name) -> str:
    """Map a product name to a category via ordered keyword rules."""
    if not name:
        return DEFAULT
    n = " " + str(name).lower() + " "
    for cat, kws in RULES:
        for kw in kws:
            if kw in n:
                return cat
    return DEFAULT


ALL_CATEGORIES = [c for c, _ in RULES] + [DEFAULT]
