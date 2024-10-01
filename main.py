import os
import psycopg2
from psycopg2 import sql
from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
import logging
import math
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)  
logger = logging.getLogger(__name__)
app = FastAPI()

url_search = "https://prod-backoffice.daribar.com/api/v2/products/search"
url_price = "https://prod-backoffice.daribar.com/api/v2/delivery/prices"
params_city = {}
# Define the payload
payload = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/best_analog")
async def main_process(request: Request):
    # Receive the front end data (city hash, sku's, user address)
    request_data = await request.json()
    encoded_city = request_data.get("city")  # Encoded city hash
    sku_data = request_data.get("skus", [])  # List of SKU items
    address = request_data.get("address", {})  # User address


    #Save the latitude and longitude of user
    user_lat = request_data.get("address", {}).get("lat")
    user_lon = request_data.get("address", {}).get("lng")
    
    # Validate the incoming data
    if not encoded_city or not sku_data or user_lat is None or user_lon is None:
        return {"error": "City, SKU data, and user coordinates are required"}
    if not encoded_city or not sku_data:
        return {"error": "City and SKU data are required"}

    # Build the payload
    payload = [{"sku": item["sku"], "count_desired": item["count_desired"]} for item in sku_data]

    # Perform the search for medicines in pharmacies
    pharmacies = await find_medicines_in_pharmacies(encoded_city, payload)

    #Save only pharmacies with all sku's in stock
    #filtered_pharmacies = await filter_pharmacies(pharmacies)

    #Save pharmacies with analogs
    analog_pharmacies = await filter_with_analogs(pharmacies)
    top_pharmacies = await sort_pharmacies_by_fulfillment(analog_pharmacies)
    closest_pharmacies = await get_top_closest_pharmacies(top_pharmacies, user_lat, user_lon)
    #cheapest_pharmacies = await get_top_cheapest_pharmacies(top_pharmacies)
    result = await get_delivery_options(closest_pharmacies)
    

    #result = await best_option(delivery_options1, delivery_options2)
    return result




async def find_medicines_in_pharmacies(encoded_city, payload):
    async with httpx.AsyncClient() as client:
        response = await client.post(url_search, params=params_city, json=payload)
        response.raise_for_status()  # Raise an error for bad responses
        return response.json()  # Return the JSON response


async def filter_with_analogs(pharmacies):
    pharmacies_with_replacements = []

    for pharmacy in pharmacies.get("result", []):
        products = pharmacy.get("products", [])
        updated_products = []  # This will hold products in stock and the cheapest analog
        total_sum = 0  # Initialize total sum for the pharmacy
        replacements_needed = 0  # Track how many replacements were made
        replaced_skus = []  # To store original and replacement SKU pairs

        # Check all products in the pharmacy
        for product in products:
            if product["quantity"] >= product["quantity_desired"]:
                # Product has sufficient stock, add its base price * quantity_desired to the total sum
                product_total_price = product["base_price"] * product["quantity_desired"]
                total_sum += product_total_price
                updated_products.append(product)  # Keep the product as is
            elif "analogs" in product and product["analogs"]:
                # Find the cheapest analog if the product is out of stock
                cheapest_analog = min(product["analogs"], key=lambda analog: analog["base_price"])
                # Create a new product entry for the analog (replacing the missing product)
                replacement_product = {
                    "source_code": cheapest_analog["source_code"],
                    "sku": cheapest_analog["sku"],
                    "name": cheapest_analog["name"],
                    "base_price": cheapest_analog["base_price"],
                    "price_with_warehouse_discount": cheapest_analog["price_with_warehouse_discount"],
                    "warehouse_discount": cheapest_analog["warehouse_discount"],
                    "quantity": cheapest_analog["quantity"],
                    "quantity_desired": product["quantity_desired"],
                    "pp_packing": cheapest_analog.get("pp_packing", ""),
                    "manufacturer_id": cheapest_analog.get("manufacturer_id", ""),
                    "recipe_needed": cheapest_analog.get("recipe_needed", False),
                    "strong_recipe": cheapest_analog.get("strong_recipe", False),
                }
                # Add the price of the cheapest analog * quantity_desired to the total sum
                analog_total_price = cheapest_analog["base_price"] * product["quantity_desired"]
                total_sum += analog_total_price
                updated_products.append(replacement_product)  # Add the analog as the product
                replacements_needed += 1

                # Track the replacement (original SKU and replacement SKU)
                replaced_skus.append({
                    "original_sku": product["sku"],
                    "replacement_sku": cheapest_analog["sku"]
                })
            else:
                # No stock and no analogs available, skip this pharmacy
                break
        else:
            # If we finish the loop without breaking, we save the pharmacy
            # Save only pharmacies where at least one replacement was made
            if replacements_needed > 0:
                pharmacies_with_replacements.append({
                    "pharmacy": {
                        "source": pharmacy["source"],  # Only include the pharmacy source info here
                        "products": updated_products,  # Keep the updated products with analogs
                        "total_sum": total_sum,  # Include total price of the pharmacy
                        "replacements_needed": replacements_needed,  # Track the number of replacements
                        "replaced_skus": replaced_skus  # Store the SKUs of original and replacements
                    }
                })

    # Return pharmacies with replacements and their updated product lists
    return {"filtered_pharmacies": pharmacies_with_replacements}



async def sort_pharmacies_by_fulfillment(pharmacies_with_replacements):
    # Sort pharmacies by the number of replacements (ascending)
    sorted_pharmacies = sorted(
        pharmacies_with_replacements.get("filtered_pharmacies", []),
        key=lambda x: x["pharmacy"]["replacements_needed"]
    )

    fewest_analogs = sorted_pharmacies[:7]
    return {"list_pharmacies": fewest_analogs}



#Find pharmacies with cheapest "total_sum" fro sku's
async def get_top_cheapest_pharmacies(pharmacies):
    # Access the list of pharmacies from the "list_pharmacies" key
    pharmacies_list = pharmacies.get("list_pharmacies", [])

    # Sort pharmacies by 'total_sum' in ascending order
    sorted_pharmacies = sorted(pharmacies_list, key=lambda x: x["pharmacy"]["total_sum"])

    # Get the top 1 pharmacy with the lowest 'total_sum'
    cheapest_pharmacies = sorted_pharmacies  # Adjust slice if you want more than one

    return {"list_pharmacies": cheapest_pharmacies}


async def get_top_closest_pharmacies(pharmacies, user_lat, user_lon):
    # Create a list of pharmacies with their distance from the user
    pharmacies_with_distance = []
    
    for item in pharmacies.get("list_pharmacies", []):
        # Access the 'pharmacy' and 'source' dictionaries safely
        pharmacy = item.get("pharmacy", {})
        source = pharmacy.get("source", {})
        pharmacy_lat = source.get("lat")
        pharmacy_lon = source.get("lon")
        
        # Check if lat/lon exist before calculating the distance
        if pharmacy_lat is None or pharmacy_lon is None:
            continue  # Skip if lat/lon is missing

        # Calculate Euclidean distance
        distance = haversine_distance(user_lat, user_lon, pharmacy_lat, pharmacy_lon)
        
        # Add the pharmacy and its distance to the list
        pharmacies_with_distance.append({"pharmacy": pharmacy, "distance": distance})
    
    # Sort pharmacies by distance
    sorted_pharmacies = sorted(pharmacies_with_distance, key=lambda x: x["distance"])
    
    # Get the top 2 closest pharmacies
    closest_pharmacies = [item["pharmacy"] for item in sorted_pharmacies[:2]]
    
    return {"list_pharmacies": closest_pharmacies}





#Algorithm to determine distance in 2 dimensions
def haversine_distance(lat1, lon1, lat2, lon2):
    distance = math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)
    return distance



async def get_delivery_options(pharmacies):
    cheapest_option = None
    fastest_option = None

    for pharmacy in pharmacies["list_pharmacies"]:
        source = pharmacy.get("source", {})
        products = pharmacy.get("products", [])

        # Ensure source code is present
        if "code" not in source:
            continue  # Skip if no source code is available

        # Build the POST request payload using the products from the pharmacy
        payload = {
            "items": [{"sku": product["sku"], "quantity": product["quantity_desired"]} for product in products],
            "dst": {
                "lat": source.get("lat"),  # Latitude of the pharmacy
                "lng": source.get("lon")   # Longitude of the pharmacy
            },
            "source_code": source["code"]  # Use the pharmacy source code
        }

        # Send the POST request to the external endpoint
        async with httpx.AsyncClient() as client:
            response = await client.post("https://prod-backoffice.daribar.com/api/v2/delivery/prices", json=payload)
            response.raise_for_status()
            delivery_data = response.json()  # Parse the JSON response

        # Extract pricing and delivery options from the response
        if delivery_data.get("status") == "success":
            items_price = delivery_data["result"]["items_price"]
            delivery_options = delivery_data["result"]["delivery"]

            # Compare for cheapest option
            for option in delivery_options:
                total_price = items_price + option["price"]  # Item price + delivery price
                if cheapest_option is None or total_price < cheapest_option["total_price"]:
                    cheapest_option = {
                        "pharmacy": pharmacy,
                        "total_price": total_price,
                        "delivery_option": option
                    }

                # Compare for fastest option
                if fastest_option is None or option["eta"] < fastest_option["delivery_option"]["eta"]:
                    fastest_option = {
                        "pharmacy": pharmacy,
                        "total_price": total_price,
                        "delivery_option": option
                    }

    return {
        "cheapest_analog_pharmacy": cheapest_option,
        "fastest_analog_pharmacy": fastest_option
    }





