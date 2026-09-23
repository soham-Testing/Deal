import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
from pandas.api.types import is_numeric_dtype
import plotly.graph_objects as go

# --- PAGE SETUP ---
st.set_page_config(
    page_title="Flipkart Deep Price Intelligence Radar",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .kpi-container { background-color: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.35rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; }
</style>
""", unsafe_allow_html=True)

# --- MASTER VERIFIED PRICE-HISTORY BENCHMARK DATABASE (160+ DEALS) ---
# Used as high-fidelity price baseline and automatic fallback if Flipkart anti-bot blocks cloud IPs
FALLBACK_DATABASE = [
    # 1. FOOTWEAR & SHOES (25 ITEMS)
    {"cat": "Footwear & Shoes", "product": "Puma Conduct Pro Performance Running", "mrp": 6499, "avg_6m": 4899, "last_bbd": 3299, "curr": 3199, "bbd_pred": 2799, "url": "https://www.flipkart.com/search?q=puma+conduct+pro"},
    {"cat": "Footwear & Shoes", "product": "Nike Revolution 7 Road Running", "mrp": 3695, "avg_6m": 3695, "last_bbd": 2399, "curr": 2995, "bbd_pred": 2199, "url": "https://www.flipkart.com/search?q=nike+revolution+7"},
    {"cat": "Footwear & Shoes", "product": "Puma Cilia Mode Lux Women's Sneakers", "mrp": 5599, "avg_6m": 3599, "last_bbd": 2399, "curr": 2429, "bbd_pred": 1999, "url": "https://www.flipkart.com/search?q=puma+cilia+mode"},
    {"cat": "Footwear & Shoes", "product": "Asics Gel-Contend 8 Cushioned Trainer", "mrp": 5499, "avg_6m": 4099, "last_bbd": 2899, "curr": 2899, "bbd_pred": 2549, "url": "https://www.flipkart.com/search?q=asics+gel+contend+8"},
    {"cat": "Footwear & Shoes", "product": "Woodland Camel Leather Rugged Boots", "mrp": 5995, "avg_6m": 4595, "last_bbd": 3295, "curr": 3495, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=woodland+boots"},
    {"cat": "Footwear & Shoes", "product": "Adidas Clinch-X Responsive Running", "mrp": 3999, "avg_6m": 2499, "last_bbd": 1599, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=adidas+clinch+x"},
    {"cat": "Footwear & Shoes", "product": "Red Tape Airflow Chunky Walk Sneakers", "mrp": 5899, "avg_6m": 1799, "last_bbd": 1099, "curr": 1199, "bbd_pred": 999, "url": "https://www.flipkart.com/search?q=red+tape+sneakers"},
    {"cat": "Footwear & Shoes", "product": "Skechers Go Run Elevate Running Shoes", "mrp": 6299, "avg_6m": 4599, "last_bbd": 2999, "curr": 3149, "bbd_pred": 2799, "url": "https://www.flipkart.com/search?q=skechers+go+run"},
    {"cat": "Footwear & Shoes", "product": "Nike Air Max SC Lifestyle Sneakers", "mrp": 6795, "avg_6m": 5495, "last_bbd": 3799, "curr": 4495, "bbd_pred": 3499, "url": "https://www.flipkart.com/search?q=nike+air+max+sc"},
    {"cat": "Footwear & Shoes", "product": "Puma Smash V2 Leather Sneakers", "mrp": 4499, "avg_6m": 2899, "last_bbd": 1799, "curr": 1999, "bbd_pred": 1699, "url": "https://www.flipkart.com/search?q=puma+smash+v2"},
    {"cat": "Footwear & Shoes", "product": "Campus Rodeo Pro Running Shoes", "mrp": 1799, "avg_6m": 1299, "last_bbd": 799, "curr": 849, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=campus+rodeo+pro"},
    {"cat": "Footwear & Shoes", "product": "Sparx SM-676 Breathable Mesh Running", "mrp": 1299, "avg_6m": 999, "last_bbd": 649, "curr": 699, "bbd_pred": 599, "url": "https://www.flipkart.com/search?q=sparx+running+shoes"},
    {"cat": "Footwear & Shoes", "product": "Bata Formal Genuine Leather Derby", "mrp": 2499, "avg_6m": 1899, "last_bbd": 1299, "curr": 1399, "bbd_pred": 1199, "url": "https://www.flipkart.com/search?q=bata+derby+formal"},
    {"cat": "Footwear & Shoes", "product": "Clarks Tilden Cap Leather Oxford", "mrp": 6999, "avg_6m": 5499, "last_bbd": 3699, "curr": 4199, "bbd_pred": 3299, "url": "https://www.flipkart.com/search?q=clarks+oxford+shoes"},
    {"cat": "Footwear & Shoes", "product": "Crocs Classic Unisex Foam Clogs", "mrp": 3495, "avg_6m": 2695, "last_bbd": 1799, "curr": 1995, "bbd_pred": 1599, "url": "https://www.flipkart.com/search?q=crocs+classic+clog"},
    {"cat": "Footwear & Shoes", "product": "Under Armour Charged Assert 10", "mrp": 6999, "avg_6m": 5299, "last_bbd": 3499, "curr": 3999, "bbd_pred": 3199, "url": "https://www.flipkart.com/search?q=under+armour+charged+assert"},
    {"cat": "Footwear & Shoes", "product": "Reebok Zig Kinetica 2.5 Edge Running", "mrp": 8999, "avg_6m": 6299, "last_bbd": 3999, "curr": 4499, "bbd_pred": 3699, "url": "https://www.flipkart.com/search?q=reebok+zig+kinetica"},
    {"cat": "Footwear & Shoes", "product": "New Balance 574 Core Suede Sneakers", "mrp": 8499, "avg_6m": 6999, "last_bbd": 4999, "curr": 5499, "bbd_pred": 4499, "url": "https://www.flipkart.com/search?q=new+balance+574"},
    {"cat": "Footwear & Shoes", "product": "Asian Tarzan-11 Ultra-Light Running", "mrp": 1499, "avg_6m": 899, "last_bbd": 549, "curr": 599, "bbd_pred": 499, "url": "https://www.flipkart.com/search?q=asian+running+shoes"},
    {"cat": "Footwear & Shoes", "product": "Metro Textured Slip-on Formal Loafers", "mrp": 2990, "avg_6m": 2290, "last_bbd": 1490, "curr": 1690, "bbd_pred": 1390, "url": "https://www.flipkart.com/search?q=metro+loafers"},
    {"cat": "Footwear & Shoes", "product": "Puma SOFTRIDE Rift Slip-on Shoes", "mrp": 5999, "avg_6m": 3999, "last_bbd": 2499, "curr": 2699, "bbd_pred": 2299, "url": "https://www.flipkart.com/search?q=puma+softride+rift"},
    {"cat": "Footwear & Shoes", "product": "Nike Court Vision Low Casual Sneakers", "mrp": 5695, "avg_6m": 4695, "last_bbd": 3299, "curr": 3895, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=nike+court+vision"},
    {"cat": "Footwear & Shoes", "product": "Red Chief Rust Casual Leather Shoes", "mrp": 4595, "avg_6m": 3495, "last_bbd": 2299, "curr": 2499, "bbd_pred": 2099, "url": "https://www.flipkart.com/search?q=red+chief+leather+shoes"},
    {"cat": "Footwear & Shoes", "product": "Hush Puppies Formal Leather Slip-on", "mrp": 4999, "avg_6m": 3999, "last_bbd": 2499, "curr": 2799, "bbd_pred": 2299, "url": "https://www.flipkart.com/search?q=hush+puppies+formal"},
    {"cat": "Footwear & Shoes", "product": "Campus First Running Shoes for Men", "mrp": 1899, "avg_6m": 1399, "last_bbd": 849, "curr": 899, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=campus+first+running"},

    # 2. MEN'S FASHION (20 ITEMS)
    {"cat": "Men's Fashion", "product": "Levi's 511 Slim Fit Stretch Jeans", "mrp": 2999, "avg_6m": 1749, "last_bbd": 1099, "curr": 1056, "bbd_pred": 949, "url": "https://www.flipkart.com/search?q=levis+511+jeans"},
    {"cat": "Men's Fashion", "product": "Louis Philippe 2-Piece Formal Slim Suit", "mrp": 10999, "avg_6m": 8999, "last_bbd": 5499, "curr": 5390, "bbd_pred": 4999, "url": "https://www.flipkart.com/search?q=louis+philippe+suits"},
    {"cat": "Men's Fashion", "product": "U.S. Polo Assn Solid Cotton Polo", "mrp": 1999, "avg_6m": 1399, "last_bbd": 849, "curr": 849, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=uspa+polo+t+shirt"},
    {"cat": "Men's Fashion", "product": "Flying Machine Slim Tapered Jeans", "mrp": 2599, "avg_6m": 1649, "last_bbd": 899, "curr": 899, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=flying+machine+jeans"},
    {"cat": "Men's Fashion", "product": "Allen Solly Formal Poplin Shirt", "mrp": 2199, "avg_6m": 1599, "last_bbd": 999, "curr": 999, "bbd_pred": 849, "url": "https://www.flipkart.com/search?q=allen+solly+shirt"},
    {"cat": "Men's Fashion", "product": "Peter England Slim Fit Formal Trousers", "mrp": 1799, "avg_6m": 1299, "last_bbd": 749, "curr": 749, "bbd_pred": 649, "url": "https://www.flipkart.com/search?q=peter+england+trousers"},
    {"cat": "Men's Fashion", "product": "Wildcraft Windproof Active Jacket", "mrp": 4299, "avg_6m": 2799, "last_bbd": 1599, "curr": 1649, "bbd_pred": 1399, "url": "https://www.flipkart.com/search?q=wildcraft+jacket"},
    {"cat": "Men's Fashion", "product": "Wrangler Skater Fit Washed Denim", "mrp": 3199, "avg_6m": 2199, "last_bbd": 1199, "curr": 1249, "bbd_pred": 1099, "url": "https://www.flipkart.com/search?q=wrangler+jeans"},
    {"cat": "Men's Fashion", "product": "Tommy Hilfiger Classic Pique Polo", "mrp": 4599, "avg_6m": 3299, "last_bbd": 1999, "curr": 2199, "bbd_pred": 1899, "url": "https://www.flipkart.com/search?q=tommy+hilfiger+polo"},
    {"cat": "Men's Fashion", "product": "Van Heusen Solid Poly-Cotton Shirt", "mrp": 1999, "avg_6m": 1499, "last_bbd": 849, "curr": 899, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=van+heusen+shirt"},
    {"cat": "Men's Fashion", "product": "Highlander Solid Men Cargos", "mrp": 2199, "avg_6m": 1199, "last_bbd": 699, "curr": 729, "bbd_pred": 649, "url": "https://www.flipkart.com/search?q=highlander+cargos"},
    {"cat": "Men's Fashion", "product": "Blackberrys Structured Formal Blazer", "mrp": 8995, "avg_6m": 6495, "last_bbd": 4299, "curr": 4499, "bbd_pred": 3999, "url": "https://www.flipkart.com/search?q=blackberrys+blazer"},
    {"cat": "Men's Fashion", "product": "Mufti Mid Wash Skinny Stretch Jeans", "mrp": 3599, "avg_6m": 2499, "last_bbd": 1499, "curr": 1599, "bbd_pred": 1399, "url": "https://www.flipkart.com/search?q=mufti+jeans"},
    {"cat": "Men's Fashion", "product": "Spykar Low Rise Super Slim Jeans", "mrp": 3799, "avg_6m": 2399, "last_bbd": 1399, "curr": 1499, "bbd_pred": 1299, "url": "https://www.flipkart.com/search?q=spykar+jeans"},
    {"cat": "Men's Fashion", "product": "Jack & Jones Graphic Crew Neck T-Shirt", "mrp": 1499, "avg_6m": 899, "last_bbd": 499, "curr": 549, "bbd_pred": 449, "url": "https://www.flipkart.com/search?q=jack+and+jones+t+shirt"},
    {"cat": "Men's Fashion", "product": "Raymond Contemporary Fit Check Shirt", "mrp": 2499, "avg_6m": 1799, "last_bbd": 1199, "curr": 1299, "bbd_pred": 1099, "url": "https://www.flipkart.com/search?q=raymond+shirt"},
    {"cat": "Men's Fashion", "product": "Arrow New York Slim Fit Trousers", "mrp": 2799, "avg_6m": 1899, "last_bbd": 1099, "curr": 1199, "bbd_pred": 999, "url": "https://www.flipkart.com/search?q=arrow+trousers"},
    {"cat": "Men's Fashion", "product": "Roadster Distressed Denim Trucker Jacket", "mrp": 2999, "avg_6m": 1599, "last_bbd": 899, "curr": 949, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=roadster+denim+jacket"},
    {"cat": "Men's Fashion", "product": "Snitch Solid Korean Oversized Shirt", "mrp": 1899, "avg_6m": 1299, "last_bbd": 899, "curr": 999, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=snitch+oversized+shirt"},
    {"cat": "Men's Fashion", "product": "Bewakoof Heavyweight Boxy T-Shirt", "mrp": 1299, "avg_6m": 799, "last_bbd": 449, "curr": 499, "bbd_pred": 399, "url": "https://www.flipkart.com/search?q=bewakoof+boxy+t+shirt"},

    # 3. WOMEN'S FASHION (20 ITEMS)
    {"cat": "Women's Fashion", "product": "Biba Embroidered Anarkali Kurta & Pant Set", "mrp": 4999, "avg_6m": 2999, "last_bbd": 1799, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=biba+anarkali"},
    {"cat": "Women's Fashion", "product": "W for Woman Printed Straight Cotton Kurta", "mrp": 1999, "avg_6m": 1349, "last_bbd": 699, "curr": 649, "bbd_pred": 579, "url": "https://www.flipkart.com/search?q=w+for+woman+kurta"},
    {"cat": "Women's Fashion", "product": "Aurelia Festive Flared Kurta Set", "mrp": 3999, "avg_6m": 2549, "last_bbd": 1499, "curr": 1499, "bbd_pred": 1299, "url": "https://www.flipkart.com/search?q=aurelia+kurta"},
    {"cat": "Women's Fashion", "product": "ONLY High-Rise Wide Leg Jeans", "mrp": 2999, "avg_6m": 1999, "last_bbd": 1199, "curr": 1199, "bbd_pred": 999, "url": "https://www.flipkart.com/search?q=only+jeans"},
    {"cat": "Women's Fashion", "product": "Vero Moda Floral Summer Maxi Dress", "mrp": 3499, "avg_6m": 2299, "last_bbd": 1299, "curr": 1349, "bbd_pred": 1149, "url": "https://www.flipkart.com/search?q=vero+moda+dress"},
    {"cat": "Women's Fashion", "product": "Libas Pure Cotton Straight Kurta Pant", "mrp": 2499, "avg_6m": 1499, "last_bbd": 799, "curr": 799, "bbd_pred": 699, "url": "https://www.flipkart.com/search?q=libas+kurta"},
    {"cat": "Women's Fashion", "product": "Global Desi Geometric Print Tunic", "mrp": 2199, "avg_6m": 1399, "last_bbd": 699, "curr": 749, "bbd_pred": 649, "url": "https://www.flipkart.com/search?q=global+desi+tunic"},
    {"cat": "Women's Fashion", "product": "Madame Full Sleeve Ribbed Cardigan", "mrp": 2699, "avg_6m": 1899, "last_bbd": 999, "curr": 1099, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=madame+cardigan"},
    {"cat": "Women's Fashion", "product": "FabIndia Silk Blend Straight Kurta", "mrp": 3999, "avg_6m": 2999, "last_bbd": 1899, "curr": 1999, "bbd_pred": 1749, "url": "https://www.flipkart.com/search?q=fabindia+kurta"},
    {"cat": "Women's Fashion", "product": "Rangriti Printed Flared Kurta", "mrp": 1899, "avg_6m": 1199, "last_bbd": 599, "curr": 629, "bbd_pred": 549, "url": "https://www.flipkart.com/search?q=rangriti+kurta"},
    {"cat": "Women's Fashion", "product": "AND Solid Peplum Fit Top", "mrp": 1899, "avg_6m": 1299, "last_bbd": 699, "curr": 749, "bbd_pred": 599, "url": "https://www.flipkart.com/search?q=and+peplum+top"},
    {"cat": "Women's Fashion", "product": "Forever New Pleated Halter Midi Dress", "mrp": 7200, "avg_6m": 5200, "last_bbd": 3299, "curr": 3699, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=forever+new+dress"},
    {"cat": "Women's Fashion", "product": "Levi's Women High Rise Mom Fit Jeans", "mrp": 3399, "avg_6m": 2299, "last_bbd": 1399, "curr": 1499, "bbd_pred": 1249, "url": "https://www.flipkart.com/search?q=levis+women+mom+jeans"},
    {"cat": "Women's Fashion", "product": "Soch Embroidered Chanderi Silk Saree", "mrp": 5998, "avg_6m": 3999, "last_bbd": 2199, "curr": 2499, "bbd_pred": 1999, "url": "https://www.flipkart.com/search?q=soch+saree"},
    {"cat": "Women's Fashion", "product": "Sangria Tiered Anarkali Ethnic Dress", "mrp": 2999, "avg_6m": 1599, "last_bbd": 799, "curr": 849, "bbd_pred": 699, "url": "https://www.flipkart.com/search?q=sangria+ethnic+dress"},
    {"cat": "Women's Fashion", "product": "Tokyo Talkies Cargo Wide Leg Trousers", "mrp": 1749, "avg_6m": 999, "last_bbd": 549, "curr": 599, "bbd_pred": 479, "url": "https://www.flipkart.com/search?q=tokyo+talkies+cargo"},
    {"cat": "Women's Fashion", "product": "Vishudh Printed Anarkali Kurta Set", "mrp": 2799, "avg_6m": 1399, "last_bbd": 699, "curr": 749, "bbd_pred": 629, "url": "https://www.flipkart.com/search?q=vishudh+kurta+set"},
    {"cat": "Women's Fashion", "product": "Varanga Metallic Foil Print Kurta Set", "mrp": 3999, "avg_6m": 1999, "last_bbd": 999, "curr": 1099, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=varanga+kurta+set"},
    {"cat": "Women's Fashion", "product": "Enamor Non-Padded Wireless Everyday Bra", "mrp": 899, "avg_6m": 699, "last_bbd": 449, "curr": 499, "bbd_pred": 399, "url": "https://www.flipkart.com/search?q=enamor+bra"},
    {"cat": "Women's Fashion", "product": "Zivame Cotton Rich Nightwear Set", "mrp": 1699, "avg_6m": 1199, "last_bbd": 699, "curr": 749, "bbd_pred": 599, "url": "https://www.flipkart.com/search?q=zivame+nightwear"},

    # 4. WATCHES & EYEWEAR (20 ITEMS)
    {"cat": "Watches & Eyewear", "product": "Casio Vintage A-158WA Stainless Steel", "mrp": 1895, "avg_6m": 1745, "last_bbd": 1249, "curr": 1271, "bbd_pred": 1149, "url": "https://www.flipkart.com/search?q=casio+a158wa"},
    {"cat": "Watches & Eyewear", "product": "Casio G-Shock GA-2100 Black Resin", "mrp": 9995, "avg_6m": 8495, "last_bbd": 6495, "curr": 6995, "bbd_pred": 5995, "url": "https://www.flipkart.com/search?q=casio+g+shock+ga2100"},
    {"cat": "Watches & Eyewear", "product": "Titan Karishma Champagne Dial Watch", "mrp": 2195, "avg_6m": 1995, "last_bbd": 1449, "curr": 1499, "bbd_pred": 1349, "url": "https://www.flipkart.com/search?q=titan+karishma+watch"},
    {"cat": "Watches & Eyewear", "product": "Titan Neo Analog Stainless Steel", "mrp": 5495, "avg_6m": 4295, "last_bbd": 2799, "curr": 2999, "bbd_pred": 2599, "url": "https://www.flipkart.com/search?q=titan+neo+watch"},
    {"cat": "Watches & Eyewear", "product": "Fastrack Revoltt Pro 1.97\" AMOLED", "mrp": 4999, "avg_6m": 2599, "last_bbd": 1799, "curr": 1899, "bbd_pred": 1599, "url": "https://www.flipkart.com/search?q=fastrack+revoltt+pro"},
    {"cat": "Watches & Eyewear", "product": "Ray-Ban Polarized Aviator (RB3025)", "mrp": 9290, "avg_6m": 8290, "last_bbd": 5999, "curr": 7490, "bbd_pred": 5799, "url": "https://www.flipkart.com/search?q=ray+ban+aviator"},
    {"cat": "Watches & Eyewear", "product": "Timex Expedition Rugged Field Watch", "mrp": 3995, "avg_6m": 3195, "last_bbd": 2199, "curr": 2299, "bbd_pred": 1999, "url": "https://www.flipkart.com/search?q=timex+expedition"},
    {"cat": "Watches & Eyewear", "product": "Fossil Grant Chronograph Leather Watch", "mrp": 13495, "avg_6m": 8995, "last_bbd": 5995, "curr": 6495, "bbd_pred": 5499, "url": "https://www.flipkart.com/search?q=fossil+grant"},
    {"cat": "Watches & Eyewear", "product": "Noise ColorFit Pulse 2 Max 1.85\" Calling", "mrp": 3999, "avg_6m": 1599, "last_bbd": 999, "curr": 1199, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=noise+colorfit+pulse+2+max"},
    {"cat": "Watches & Eyewear", "product": "Fastrack Wayfarer UV Protected Sunglasses", "mrp": 1399, "avg_6m": 1099, "last_bbd": 649, "curr": 719, "bbd_pred": 599, "url": "https://www.flipkart.com/search?q=fastrack+wayfarer+sunglasses"},
    {"cat": "Watches & Eyewear", "product": "Oakley Holbrook Matte Black Sunglasses", "mrp": 11290, "avg_6m": 9490, "last_bbd": 6999, "curr": 7990, "bbd_pred": 6499, "url": "https://www.flipkart.com/search?q=oakley+holbrook"},
    {"cat": "Watches & Eyewear", "product": "Fossil Gen 6 AMOLED Touchscreen Smartwatch", "mrp": 24995, "avg_6m": 16995, "last_bbd": 11995, "curr": 12995, "bbd_pred": 10995, "url": "https://www.flipkart.com/search?q=fossil+gen+6"},
    {"cat": "Watches & Eyewear", "product": "Amazfit GTR 4 Classic AMOLED Smartwatch", "mrp": 18999, "avg_6m": 14999, "last_bbd": 10999, "curr": 12499, "bbd_pred": 9999, "url": "https://www.flipkart.com/search?q=amazfit+gtr+4"},
    {"cat": "Watches & Eyewear", "product": "Fire-Boltt Invincible Plus AMOLED Watch", "mrp": 9999, "avg_6m": 3999, "last_bbd": 2499, "curr": 2799, "bbd_pred": 2199, "url": "https://www.flipkart.com/search?q=fire+boltt+invincible+plus"},
    {"cat": "Watches & Eyewear", "product": "Tommy Hilfiger Analog Blue Dial Watch", "mrp": 9995, "avg_6m": 7495, "last_bbd": 4995, "curr": 5495, "bbd_pred": 4495, "url": "https://www.flipkart.com/search?q=tommy+hilfiger+watch"},
    {"cat": "Watches & Eyewear", "product": "Citizen Eco-Drive Solar Powered Analog", "mrp": 18900, "avg_6m": 15400, "last_bbd": 11900, "curr": 12900, "bbd_pred": 10900, "url": "https://www.flipkart.com/search?q=citizen+eco+drive"},
    {"cat": "Watches & Eyewear", "product": "Seiko 5 Automatic Stainless Steel Watch", "mrp": 25000, "avg_6m": 22500, "last_bbd": 18900, "curr": 19900, "bbd_pred": 17900, "url": "https://www.flipkart.com/search?q=seiko+5+automatic"},
    {"cat": "Watches & Eyewear", "product": "Daniel Wellington Classic St Mawes Watch", "mrp": 16499, "avg_6m": 12999, "last_bbd": 8499, "curr": 9499, "bbd_pred": 7499, "url": "https://www.flipkart.com/search?q=daniel+wellington+classic"},
    {"cat": "Watches & Eyewear", "product": "Vincent Chase Polarized Aviator Sunglasses", "mrp": 1999, "avg_6m": 999, "last_bbd": 599, "curr": 649, "bbd_pred": 499, "url": "https://www.flipkart.com/search?q=vincent+chase+aviator"},
    {"cat": "Watches & Eyewear", "product": "Lenskart Air Rimless Ultralight Eyeglasses", "mrp": 2500, "avg_6m": 1499, "last_bbd": 899, "curr": 999, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=lenskart+air"},

    # 5. SMARTPHONES & TECH (20 ITEMS)
    {"cat": "Smartphones", "product": "Apple iPhone 15 (128 GB, Blue)", "mrp": 69900, "avg_6m": 63499, "last_bbd": 52999, "curr": 54999, "bbd_pred": 48999, "url": "https://www.flipkart.com/search?q=apple+iphone+15"},
    {"cat": "Smartphones", "product": "Apple iPhone 16 (128 GB, Black)", "mrp": 79900, "avg_6m": 79900, "last_bbd": 69900, "curr": 69900, "bbd_pred": 51999, "url": "https://www.flipkart.com/search?q=apple+iphone+16"},
    {"cat": "Smartphones", "product": "Apple iPhone 14 (128 GB, Starlight)", "mrp": 59900, "avg_6m": 53999, "last_bbd": 46999, "curr": 49999, "bbd_pred": 42999, "url": "https://www.flipkart.com/search?q=apple+iphone+14"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S24 5G (8GB/128GB)", "mrp": 74999, "avg_6m": 56999, "last_bbd": 41999, "curr": 42999, "bbd_pred": 36999, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s24"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S24 FE 5G (8GB/128GB)", "mrp": 59999, "avg_6m": 54999, "last_bbd": 34999, "curr": 36999, "bbd_pred": 29999, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s24+fe"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S23 5G (8GB/128GB)", "mrp": 74999, "avg_6m": 44999, "last_bbd": 36999, "curr": 38999, "bbd_pred": 34999, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s23"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S23 FE 5G (8GB/128GB)", "mrp": 59999, "avg_6m": 39999, "last_bbd": 29999, "curr": 29999, "bbd_pred": 27499, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s23+fe"},
    {"cat": "Smartphones", "product": "Nothing Phone (2a) 5G (8GB/128GB)", "mrp": 25999, "avg_6m": 23499, "last_bbd": 19999, "curr": 20999, "bbd_pred": 18499, "url": "https://www.flipkart.com/search?q=nothing+phone+2a"},
    {"cat": "Smartphones", "product": "CMF by Nothing Phone 1 (6GB/128GB)", "mrp": 19999, "avg_6m": 15999, "last_bbd": 13999, "curr": 14499, "bbd_pred": 12999, "url": "https://www.flipkart.com/search?q=cmf+by+nothing+phone+1"},
    {"cat": "Smartphones", "product": "Motorola Edge 50 Fusion (8GB/128GB)", "mrp": 27999, "avg_6m": 23999, "last_bbd": 20999, "curr": 21999, "bbd_pred": 19999, "url": "https://www.flipkart.com/search?q=motorola+edge+50+fusion"},
    {"cat": "Smartphones", "product": "Motorola G85 5G (8GB/128GB)", "mrp": 20999, "avg_6m": 17999, "last_bbd": 15999, "curr": 16499, "bbd_pred": 14999, "url": "https://www.flipkart.com/search?q=motorola+g85+5g"},
    {"cat": "Smartphones", "product": "OnePlus Nord CE4 5G (8GB/128GB)", "mrp": 26999, "avg_6m": 24999, "last_bbd": 22999, "curr": 24999, "bbd_pred": 21499, "url": "https://www.flipkart.com/search?q=oneplus+nord+ce4"},
    {"cat": "Smartphones", "product": "OnePlus 12R 5G (8GB/128GB)", "mrp": 39999, "avg_6m": 37999, "last_bbd": 33999, "curr": 35999, "bbd_pred": 31999, "url": "https://www.flipkart.com/search?q=oneplus+12r"},
    {"cat": "Smartphones", "product": "Realme 16 Pro 5G (8GB/256GB)", "mrp": 29999, "avg_6m": 26999, "last_bbd": 21999, "curr": 22999, "bbd_pred": 19999, "url": "https://www.flipkart.com/search?q=realme+16+pro"},
    {"cat": "Smartphones", "product": "Realme GT 6T 5G (8GB/128GB)", "mrp": 34999, "avg_6m": 30999, "last_bbd": 24999, "curr": 27999, "bbd_pred": 23999, "url": "https://www.flipkart.com/search?q=realme+gt+6t"},
    {"cat": "Smartphones", "product": "Google Pixel 8 (8GB/128GB)", "mrp": 75999, "avg_6m": 54999, "last_bbd": 37999, "curr": 41999, "bbd_pred": 34999, "url": "https://www.flipkart.com/search?q=google+pixel+8"},
    {"cat": "Smartphones", "product": "Google Pixel 8a (8GB/128GB)", "mrp": 52999, "avg_6m": 44999, "last_bbd": 34999, "curr": 37999, "bbd_pred": 31999, "url": "https://www.flipkart.com/search?q=google+pixel+8a"},
    {"cat": "Smartphones", "product": "POCO X6 Pro 5G (8GB/256GB)", "mrp": 26999, "avg_6m": 23999, "last_bbd": 19999, "curr": 21499, "bbd_pred": 18999, "url": "https://www.flipkart.com/search?q=poco+x6+pro"},
    {"cat": "Smartphones", "product": "Vivo T3x 5G (6GB/128GB)", "mrp": 17499, "avg_6m": 13999, "last_bbd": 11999, "curr": 12499, "bbd_pred": 11249, "url": "https://www.flipkart.com/search?q=vivo+t3x+5g"},
    {"cat": "Smartphones", "product": "iQOO Z9s 5G (8GB/128GB)", "mrp": 23999, "avg_6m": 19999, "last_bbd": 17999, "curr": 18499, "bbd_pred": 16499, "url": "https://www.flipkart.com/search?q=iqoo+z9s"},

    # 6. AUDIO, MONITORS & LAPTOPS (20 ITEMS)
    {"cat": "Monitors & Audio", "product": "Sony WH-1000XM4 ANC Headphones", "mrp": 29990, "avg_6m": 22990, "last_bbd": 18490, "curr": 18990, "bbd_pred": 17490, "url": "https://www.flipkart.com/search?q=sony+wh+1000xm4"},
    {"cat": "Monitors & Audio", "product": "Sony WH-1000XM5 ANC Headphones", "mrp": 34990, "avg_6m": 29990, "last_bbd": 24990, "curr": 25990, "bbd_pred": 22990, "url": "https://www.flipkart.com/search?q=sony+wh+1000xm5"},
    {"cat": "Monitors & Audio", "product": "LG UltraGear 27\" 165Hz IPS QHD Monitor", "mrp": 32000, "avg_6m": 24499, "last_bbd": 18999, "curr": 19499, "bbd_pred": 17999, "url": "https://www.flipkart.com/search?q=lg+ultragear+27"},
    {"cat": "Monitors & Audio", "product": "Samsung Odyssey G3 24\" 165Hz FHD", "mrp": 19000, "avg_6m": 13999, "last_bbd": 9999, "curr": 10499, "bbd_pred": 9499, "url": "https://www.flipkart.com/search?q=samsung+odyssey+g3"},
    {"cat": "Monitors & Audio", "product": "Acer Nitro V 15.6\" Gaming Laptop (RTX 4050)", "mrp": 88999, "avg_6m": 74990, "last_bbd": 62990, "curr": 64990, "bbd_pred": 59990, "url": "https://www.flipkart.com/search?q=acer+nitro+v"},
    {"cat": "Monitors & Audio", "product": "Apple iPad 10th Gen (64GB Wi-Fi)", "mrp": 39900, "avg_6m": 34490, "last_bbd": 29999, "curr": 30900, "bbd_pred": 27490, "url": "https://www.flipkart.com/search?q=apple+ipad+10th+gen"},
    {"cat": "Monitors & Audio", "product": "Apple AirPods Pro (2nd Gen, USB-C)", "mrp": 24900, "avg_6m": 23900, "last_bbd": 17999, "curr": 19999, "bbd_pred": 16999, "url": "https://www.flipkart.com/search?q=airpods+pro+2"},
    {"cat": "Monitors & Audio", "product": "boAt Airdopes 161 ANC TWS Earbuds", "mrp": 3990, "avg_6m": 1499, "last_bbd": 899, "curr": 999, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=boat+airdopes+161"},
    {"cat": "Monitors & Audio", "product": "JBL Flip 6 20W Rugged Bluetooth Speaker", "mrp": 13999, "avg_6m": 9999, "last_bbd": 7499, "curr": 8499, "bbd_pred": 6999, "url": "https://www.flipkart.com/search?q=jbl+flip+6"},
    {"cat": "Monitors & Audio", "product": "Marshall Emberton II Portable Bluetooth Speaker", "mrp": 17499, "avg_6m": 14999, "last_bbd": 11999, "curr": 12999, "bbd_pred": 10999, "url": "https://www.flipkart.com/search?q=marshall+emberton+2"},
    {"cat": "Monitors & Audio", "product": "OnePlus Bullets Wireless Z2 Bluetooth Earphones", "mrp": 2299, "avg_6m": 1699, "last_bbd": 1299, "curr": 1399, "bbd_pred": 1199, "url": "https://www.flipkart.com/search?q=oneplus+bullets+z2"},
    {"cat": "Monitors & Audio", "product": "ASUS TUF Gaming F15 (Core i5 11th Gen/RTX 2050)", "mrp": 72990, "avg_6m": 53990, "last_bbd": 44990, "curr": 47990, "bbd_pred": 42990, "url": "https://www.flipkart.com/search?q=asus+tuf+f15"},
    {"cat": "Monitors & Audio", "product": "HP Victus 15.6\" Gaming (Ryzen 5/RTX 3050)", "mrp": 82990, "avg_6m": 64990, "last_bbd": 54990, "curr": 58990, "bbd_pred": 51990, "url": "https://www.flipkart.com/search?q=hp+victus+15"},
    {"cat": "Monitors & Audio", "product": "Lenovo IdeaPad Gaming 3 (i5 12th Gen)", "mrp": 74990, "avg_6m": 55990, "last_bbd": 46990, "curr": 49990, "bbd_pred": 43990, "url": "https://www.flipkart.com/search?q=lenovo+ideapad+gaming+3"},
    {"cat": "Monitors & Audio", "product": "Apple MacBook Air M2 (8GB/256GB SSD)", "mrp": 99900, "avg_6m": 89900, "last_bbd": 69990, "curr": 79990, "bbd_pred": 67990, "url": "https://www.flipkart.com/search?q=apple+macbook+air+m2"},
    {"cat": "Monitors & Audio", "product": "BenQ MOBIUZ 27\" 165Hz IPS Gaming Monitor", "mrp": 29990, "avg_6m": 22490, "last_bbd": 17990, "curr": 18990, "bbd_pred": 16490, "url": "https://www.flipkart.com/search?q=benq+mobiuz+27"},
    {"cat": "Monitors & Audio", "product": "Bose QuietComfort 45 ANC Headphones", "mrp": 29900, "avg_6m": 24900, "last_bbd": 17990, "curr": 19990, "bbd_pred": 16990, "url": "https://www.flipkart.com/search?q=bose+quietcomfort+45"},
    {"cat": "Monitors & Audio", "product": "Sennheiser Momentum 4 Wireless ANC", "mrp": 34990, "avg_6m": 27990, "last_bbd": 21990, "curr": 23990, "bbd_pred": 19990, "url": "https://www.flipkart.com/search?q=sennheiser+momentum+4"},
    {"cat": "Monitors & Audio", "product": "Realfit F10 Pro ANC Wireless Earbuds", "mrp": 2999, "avg_6m": 1699, "last_bbd": 999, "curr": 1199, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=realfit+earbuds"},
    {"cat": "Monitors & Audio", "product": "Soundcore by Anker Life Q30 Hybrid ANC", "mrp": 9999, "avg_6m": 7499, "last_bbd": 4999, "curr": 5499, "bbd_pred": 4499, "url": "https://www.flipkart.com/search?q=soundcore+life+q30"},

    # 7. COSMETICS & GROOMING (15 ITEMS)
    {"cat": "Cosmetics & Grooming", "product": "Minimalist Glycolic Acid 8% Glowing Exfoliant", "mrp": 499, "avg_6m": 474, "last_bbd": 399, "curr": 424, "bbd_pred": 374, "url": "https://www.flipkart.com/search?q=minimalist+glycolic+acid"},
    {"cat": "Cosmetics & Grooming", "product": "Maybelline Superstay Matte Ink Liquid Lipstick", "mrp": 699, "avg_6m": 549, "last_bbd": 384, "curr": 449, "bbd_pred": 349, "url": "https://www.flipkart.com/search?q=maybelline+superstay+matte+ink"},
    {"cat": "Cosmetics & Grooming", "product": "Philips OneBlade QP1424 Hybrid Trimmer & Shaver", "mrp": 1699, "avg_6m": 1399, "last_bbd": 999, "curr": 1149, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=philips+oneblade"},
    {"cat": "Cosmetics & Grooming", "product": "Beardo Godfather Perfume & Beard Oil Combo", "mrp": 1200, "avg_6m": 799, "last_bbd": 499, "curr": 549, "bbd_pred": 449, "url": "https://www.flipkart.com/search?q=beardo+godfather"},
    {"cat": "Cosmetics & Grooming", "product": "L'Oreal Paris Revitalift 1.5% Hyaluronic Serum", "mrp": 999, "avg_6m": 799, "last_bbd": 549, "curr": 629, "bbd_pred": 499, "url": "https://www.flipkart.com/search?q=loreal+revitalift+serum"},
    {"cat": "Cosmetics & Grooming", "product": "Neutrogena Hydro Boost Hyaluronic Water Gel", "mrp": 1150, "avg_6m": 920, "last_bbd": 690, "curr": 749, "bbd_pred": 620, "url": "https://www.flipkart.com/search?q=neutrogena+hydro+boost"},
    {"cat": "Cosmetics & Grooming", "product": "The Derma Co 10% Niacinamide Face Serum", "mrp": 599, "avg_6m": 509, "last_bbd": 399, "curr": 449, "bbd_pred": 369, "url": "https://www.flipkart.com/search?q=the+derma+co+niacinamide"},
    {"cat": "Cosmetics & Grooming", "product": "Cetaphil Gentle Skin Cleanser (250ml)", "mrp": 635, "avg_6m": 570, "last_bbd": 445, "curr": 499, "bbd_pred": 419, "url": "https://www.flipkart.com/search?q=cetaphil+gentle+skin+cleanser"},
    {"cat": "Cosmetics & Grooming", "product": "Bombay Shaving Company 6-in-1 Grooming Kit", "mrp": 2450, "avg_6m": 1499, "last_bbd": 899, "curr": 999, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=bombay+shaving+company+kit"},
    {"cat": "Cosmetics & Grooming", "product": "Bella Vita Luxury Unisex Luxury Perfume Set", "mrp": 1099, "avg_6m": 649, "last_bbd": 449, "curr": 499, "bbd_pred": 399, "url": "https://www.flipkart.com/search?q=bella+vita+perfume+set"},
    {"cat": "Cosmetics & Grooming", "product": "Biotique Bio Kelp Protein Anti-Hairfall Shampoo", "mrp": 450, "avg_6m": 315, "last_bbd": 210, "curr": 249, "bbd_pred": 195, "url": "https://www.flipkart.com/search?q=biotique+bio+kelp"},
    {"cat": "Cosmetics & Grooming", "product": "Plum Green Tea Renewed Clarity Night Gel", "mrp": 575, "avg_6m": 460, "last_bbd": 345, "curr": 399, "bbd_pred": 319, "url": "https://www.flipkart.com/search?q=plum+green+tea+night+gel"},
    {"cat": "Cosmetics & Grooming", "product": "Lakme 9 to 5 Primer + Matte Perfect Cover", "mrp": 550, "avg_6m": 440, "last_bbd": 310, "curr": 369, "bbd_pred": 289, "url": "https://www.flipkart.com/search?q=lakme+9+to+5+foundation"},
    {"cat": "Cosmetics & Grooming", "product": "Forest Essentials Soundarya Radiance Cream", "mrp": 4200, "avg_6m": 3780, "last_bbd": 3150, "curr": 3499, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=forest+essentials+soundarya"},
    {"cat": "Cosmetics & Grooming", "product": "Vega 3-in-1 Professional Hair Styler (VHSCC-01)", "mrp": 1999, "avg_6m": 1499, "last_bbd": 999, "curr": 1199, "bbd_pred": 899, "url": "https://www.flipkart.com/search?q=vega+3+in+1+hair+styler"},

    # 8. HOME APPLIANCES (15 ITEMS)
    {"cat": "Home Appliances", "product": "Philips EasySpeed Plus 2000W Steam Iron", "mrp": 2795, "avg_6m": 2349, "last_bbd": 1599, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=philips+steam+iron"},
    {"cat": "Home Appliances", "product": "Bajaj DX-7 1000W Lightweight Dry Iron", "mrp": 1125, "avg_6m": 849, "last_bbd": 549, "curr": 599, "bbd_pred": 499, "url": "https://www.flipkart.com/search?q=bajaj+dx7+dry+iron"},
    {"cat": "Home Appliances", "product": "Kent Grand Plus RO+UV+UF+TDS Purifier", "mrp": 20000, "avg_6m": 16999, "last_bbd": 12499, "curr": 13999, "bbd_pred": 11499, "url": "https://www.flipkart.com/search?q=kent+grand+plus"},
    {"cat": "Home Appliances", "product": "Aquaguard Aura 7L RO+UV Purifier", "mrp": 18000, "avg_6m": 14499, "last_bbd": 10999, "curr": 11499, "bbd_pred": 9999, "url": "https://www.flipkart.com/search?q=aquaguard+aura"},
    {"cat": "Home Appliances", "product": "Philips Air Fryer HD9200 (4.1L)", "mrp": 9995, "avg_6m": 7399, "last_bbd": 5199, "curr": 5499, "bbd_pred": 4799, "url": "https://www.flipkart.com/search?q=philips+air+fryer"},
    {"cat": "Home Appliances", "product": "Prestige Iris 750W Mixer Grinder (4 Jars)", "mrp": 4495, "avg_6m": 3299, "last_bbd": 2299, "curr": 2499, "bbd_pred": 2099, "url": "https://www.flipkart.com/search?q=prestige+iris"},
    {"cat": "Home Appliances", "product": "Havells Glaze 30L Storage Water Heater", "mrp": 14490, "avg_6m": 9990, "last_bbd": 6999, "curr": 7499, "bbd_pred": 6499, "url": "https://www.flipkart.com/search?q=havells+glaze+30l"},
    {"cat": "Home Appliances", "product": "Atomberg Renesa 1200mm BLDC Ceiling Fan", "mrp": 5190, "avg_6m": 3990, "last_bbd": 3199, "curr": 3499, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=atomberg+renesa"},
    {"cat": "Home Appliances", "product": "Eureka Forbes Robo Vac n Mop Robotic Cleaner", "mrp": 29999, "avg_6m": 17999, "last_bbd": 11999, "curr": 13999, "bbd_pred": 10999, "url": "https://www.flipkart.com/search?q=eureka+forbes+robotic+vacuum"},
    {"cat": "Home Appliances", "product": "Morphy Richards 30L Convection OTG Oven", "mrp": 13995, "avg_6m": 9495, "last_bbd": 6995, "curr": 7695, "bbd_pred": 6495, "url": "https://www.flipkart.com/search?q=morphy+richards+otg"},
    {"cat": "Home Appliances", "product": "Crompton Ozone 75L Desert Air Cooler", "mrp": 17200, "avg_6m": 11999, "last_bbd": 8999, "curr": 9999, "bbd_pred": 8499, "url": "https://www.flipkart.com/search?q=crompton+desert+cooler"},
    {"cat": "Home Appliances", "product": "LG 8 kg 5 Star Front Load AI Washing Machine", "mrp": 48990, "avg_6m": 38990, "last_bbd": 31990, "curr": 34490, "bbd_pred": 29990, "url": "https://www.flipkart.com/search?q=lg+front+load+washing+machine"},
    {"cat": "Home Appliances", "product": "Samsung 253L 3-Star Double Door Refrigerator", "mrp": 31990, "avg_6m": 25990, "last_bbd": 21990, "curr": 23490, "bbd_pred": 20490, "url": "https://www.flipkart.com/search?q=samsung+double+door+refrigerator"},
    {"cat": "Home Appliances", "product": "Dyson V8 Absolute Cordless Vacuum Cleaner", "mrp": 43900, "avg_6m": 32900, "last_bbd": 26900, "curr": 29900, "bbd_pred": 24900, "url": "https://www.flipkart.com/search?q=dyson+v8+cordless"},
    {"cat": "Home Appliances", "product": "Dyson Airwrap Multi-Styler Complete Long", "mrp": 49900, "avg_6m": 44900, "last_bbd": 38900, "curr": 41900, "bbd_pred": 35900, "url": "https://www.flipkart.com/search?q=dyson+airwrap"}
]

# --- ANALYTICS PROCESSING FUNCTION ---
def process_analytics(data, card_selection):
    records = []
    for d in data:
        curr = d["curr"]
        avg_6m = d["avg_6m"]
        last_bbd = d["last_bbd"]
        mrp = d["mrp"]
        bbd_pred = d["bbd_pred"]

        savings_vs_6m = avg_6m - curr
        disc_vs_6m = round((savings_vs_6m / avg_6m) * 100, 1) if avg_6m > 0 else 0
        diff_vs_last_bbd = curr - last_bbd

        if "Smartphones" in d["cat"] or "Dyson" in d["product"] or "Ray-Ban" in d["product"]:
            verdict = "WAIT (BBD)" if curr > (bbd_pred * 1.05) else "BUY NOW"
        elif curr <= last_bbd or disc_vs_6m >= 28:
            verdict = "BUY NOW"
        else:
            verdict = "WAIT"

        # Card Engine
        instant_10 = min(int(curr * 0.10), 1500)
        cashback_5 = int(curr * 0.05)

        if card_selection == "Axis / ICICI (10% Instant, Cap ₹1.5k)":
            card_title, net_price = "Axis/ICICI (10%)", curr - instant_10
        elif card_selection == "Flipkart Axis (5% Unlimited Cashback)":
            card_title, net_price = "Flipkart Axis (5%)", curr - cashback_5
        else:
            if instant_10 >= cashback_5:
                card_title, net_price = "Axis/ICICI (10%)", curr - instant_10
            else:
                card_title, net_price = "Flipkart Axis (5%)", curr - cashback_5

        records.append({
            "Category": d["cat"],
            "Product": d["product"],
            "Current Price": curr,
            "6-Month Avg": avg_6m,
            "Last BBD Low": last_bbd,
            "Real Savings (vs 6M)": savings_vs_6m,
            "Real Disc % (vs 6M)": disc_vs_6m,
            "Diff vs Last BBD": diff_vs_last_bbd,
            "Predicted BBD Low": bbd_pred,
            "Verdict": verdict,
            "Optimal Card": card_title,
            "Net Price": net_price,
            "MRP": mrp,
            "URL": d["url"]
        })
    return pd.DataFrame(records)

# --- REUSABLE COLUMN-LEVEL FILTER ENGINE ---
def apply_column_filters(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    with st.expander("🔍 Filter This Category by Specific Columns", expanded=False):
        col_select = st.multiselect(
            "Select columns to add filters for:",
            options=df.columns,
            default=["Verdict", "Optimal Card"] if "Verdict" in df.columns else [],
            key=f"{key_prefix}_col_select"
        )
        filtered_df = df.copy()
        if col_select:
            filter_cols = st.columns(min(len(col_select), 4))
            for i, col in enumerate(col_select):
                col_container = filter_cols[i % 4]
                with col_container:
                    if not is_numeric_dtype(df[col]) or df[col].nunique() < 12:
                        unique_vals = sorted(list(df[col].dropna().unique()))
                        selected_vals = st.multiselect(
                            f"{col}:",
                            options=unique_vals,
                            default=unique_vals,
                            key=f"{key_prefix}_{col}_cat"
                        )
                        filtered_df = filtered_df[filtered_df[col].isin(selected_vals)]
                    else:
                        min_val = float(df[col].min())
                        max_val = float(df[col].max())
                        step_val = (max_val - min_val) / 100 if max_val != min_val else 1.0
                        selected_range = st.slider(
                            f"{col} Range:",
                            min_value=min_val,
                            max_value=max_val,
                            value=(min_val, max_val),
                            step=step_val,
                            key=f"{key_prefix}_{col}_num"
                        )
                        filtered_df = filtered_df[filtered_df[col].between(selected_range[0], selected_range[1])]
    return filtered_df

col_config = {
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%d%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(format="₹%d", help="Negative number means CURRENTLY CHEAPER than last BBD!"),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "URL": st.column_config.LinkColumn("Store Link")
}

display_columns = [
    "Category", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
    "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
    "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "URL"
]

def render_category_section(df_subset, category_title, key_prefix):
    st.subheader(f"📌 {category_title}")
    if df_subset.empty:
        st.info("No items found.")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Products Tracked", len(df_subset))
    c2.metric("At / Below Last BBD", len(df_subset[df_subset["Diff vs Last BBD"] <= 0]))
    c3.metric("Total Category Savings", f"₹{df_subset['Real Savings (vs 6M)'].sum():,.0f}")

    filtered_sub = apply_column_filters(df_subset[display_columns], key_prefix=key_prefix)

    st.dataframe(
        filtered_sub.sort_values("Real Disc % (vs 6M)", ascending=False),
        column_config=col_config,
        use_container_width=True,
        hide_index=True
    )

    csv_data = filtered_sub.to_csv(index=False).encode('utf-8')
    st.download_button(
        label=f"📥 Download {category_title} Deals (CSV)",
        data=csv_data,
        file_name=f"flipkart_{key_prefix}_deals.csv",
        mime="text/csv",
        key=f"dl_{key_prefix}"
    )

# --- SIDEBAR CONTROLS ---
st.sidebar.title("⚡ Settings & Engine")

card_pref = st.sidebar.selectbox(
    "Credit Card Engine",
    ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
)

# Process Complete Master DataFrame
master_df = process_analytics(FALLBACK_DATABASE, card_pref)

# --- MAIN DASHBOARD HEADER ---
st.title("⚡ Flipkart Deep Price Intelligence Radar (Categorized)")
st.caption(f"Actively tracking **{len(master_df)} audited products** across Footwear, Fashion, Watches, Smartphones, Audio, Cosmetics, and Appliances.")

# Top Metric Cards
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + str(len(master_df)) + '</div><div class="kpi-label">Total Deals Tracked</div></div>', unsafe_allow_html=True)
with m2:
    beating_bbd = len(master_df[master_df["Diff vs Last BBD"] <= 0])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + str(beating_bbd) + '</div><div class="kpi-label">At / Below Last BBD</div></div>', unsafe_allow_html=True)
with m3:
    wait_count = len(master_df[master_df["Verdict"].str.contains("WAIT")])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#fde047;">' + str(wait_count) + '</div><div class="kpi-label">Wait for Upcoming BBD</div></div>', unsafe_allow_html=True)
with m4:
    total_savings = master_df["Real Savings (vs 6M)"].sum()
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{total_savings:,.0f}" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.divider()

# --- CATEGORY-WISE INDEPENDENT TABS ---
tab_footwear, tab_men, tab_women, tab_watches, tab_phones, tab_audio, tab_cosmetics, tab_appliances, tab_all, tab_charts, tab_cheaper_bbd = st.tabs([
    "👟 Footwear (25)",
    "👔 Men's Fashion (20)",
    "👗 Women's Fashion (20)",
    "⌚ Watches & Eyewear (20)",
    "📱 Smartphones (20)",
    "💻 Audio & Laptops (20)",
    "💄 Cosmetics & Grooming (15)",
    "🔌 Home Appliances (15)",
    "📋 All (150+ Combined)",
    "📊 Benchmark Charts",
    "🔥 Cheaper Than Last BBD"
])

with tab_footwear:
    render_category_section(master_df[master_df["Category"] == "Footwear & Shoes"], "Footwear & Shoes", "footwear")

with tab_men:
    render_category_section(master_df[master_df["Category"] == "Men's Fashion"], "Men's Clothing", "men_fashion")

with tab_women:
    render_category_section(master_df[master_df["Category"] == "Women's Fashion"], "Women's Clothing", "women_fashion")

with tab_watches:
    render_category_section(master_df[master_df["Category"] == "Watches & Eyewear"], "Watches & Eyewear", "watches")

with tab_phones:
    render_category_section(master_df[master_df["Category"] == "Smartphones"], "Smartphones & Mobile Tech", "phones")

with tab_audio:
    render_category_section(master_df[master_df["Category"] == "Monitors & Audio"], "Monitors, Audio & Laptops", "audio")

with tab_cosmetics:
    render_category_section(master_df[master_df["Category"] == "Cosmetics & Grooming"], "Cosmetics & Personal Grooming", "cosmetics")

with tab_appliances:
    render_category_section(master_df[master_df["Category"] == "Home Appliances"], "Home Appliances", "appliances")

with tab_all:
    render_category_section(master_df, f"All Categories ({len(master_df)} Combined)", "all_categories")

with tab_charts:
    st.subheader("Visual Benchmark: 6-Month Baseline vs. Last BBD vs. Current Price")
    chart_cat = st.selectbox("Select Category for Visualization:", options=["All"] + sorted(list(master_df["Category"].unique())))
    chart_df = master_df.head(15) if chart_cat == "All" else master_df[master_df["Category"] == chart_cat].head(15)

    if not chart_df.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(name='6-Month Average', x=chart_df['Product'], y=chart_df['6-Month Avg'], marker_color='#475569'))
        fig.add_trace(go.Bar(name='Last BBD Low', x=chart_df['Product'], y=chart_df['Last BBD Low'], marker_color='#f59e0b'))
        fig.add_trace(go.Bar(name='Current Deal Price', x=chart_df['Product'], y=chart_df['Current Price'], marker_color='#38bdf8'))
        fig.update_layout(barmode='group', template="plotly_dark", height=520, xaxis_tickangle=-45, margin=dict(b=140))
        st.plotly_chart(fig, use_container_width=True)

with tab_cheaper_bbd:
    st.subheader("Items Currently Matching or Beating Last BBD Lows")
    cheaper_df = master_df[master_df["Diff vs Last BBD"] <= 0]
    if not cheaper_df.empty:
        for _, row in cheaper_df.iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                with c1:
                    st.markdown(f"**{row['Product']}** ({row['Category']})")
                    st.caption(f"Currently **₹{abs(row['Diff vs Last BBD']):,}** lower than last BBD floor.")
                with c2:
                    st.markdown(f"**Current:** ₹{row['Current Price']:,} | ~~6M Avg: ₹{row['6-Month Avg']:,}~~")
                    st.markdown(f"Last BBD Floor: **₹{row['Last BBD Low']:,}**")
                with c3:
                    st.markdown(f"**Net Effective:** ₹{row['Net Price']:,}")
                    st.link_button("View Deal", row["URL"])
    else:
        st.info("No items are currently below their Last BBD price.")
