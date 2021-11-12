# packages
import ee
import datetime
import json
import sys
from fpdf import FPDF
import folium
import selenium
from selenium.webdriver.firefox.options import Options as FirefoxOptions

# get timeframe through command line arguments sys.argv[1]
timeframe = 'one_year'
IMAGE_WIDTH = 2480
IMAGE_HEIGHT = 1748

# ee.Authenticate()
ee.Initialize()

# variables
## import AOI and set geometry
with open('DQ.geojson') as f:
    data = json.load(f)
geometry = data['features'][0]['geometry']

## set dates for analysis
py_date = datetime.datetime.utcnow()
ee_date = ee.Date(py_date)
# print(ee_date)
if timeframe == 'two_weeks':
    start_date = ee.Date(py_date - datetime.timedelta(days=14))
    end_date = ee_date
elif timeframe == 'one_year':
    current_year = py_date.year
    start_date = ee.Date(py_date.replace(year=current_year - 1))
    end_date = ee_date
elif timeframe == 'nov_2016':
    start_date = ee.Date(py_date.replace(year=2016, month=11))
    end_date = ee.Date(py_date.replace(month=11))
elif timeframe == 'july_2016':
    start_date = ee.Date(py_date.replace(year=2016, month=7))
    end_date = ee.Date(py_date.replace(month=7))
else:
    print(f'Command {timeframe} not found.')


# cloud masking function
def mask_cloud_and_shadows(image):
    qa = image.select('QA60')

    # Both flags should be set to zero, indicating clear conditions
    clouds = qa.bitwiseAnd(1 << 10).Or(qa.bitwiseAnd(1 << 11))

    return image.updateMask(clouds.Not())


# NDVI function
def add_NDVI(image):
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('ndvi')
    ndvi02 = ndvi.gt(0.2)
    ndvi_img = image.addBands(ndvi).updateMask(ndvi02)
    ndvi02_area = ndvi02.multiply(ee.Image.pixelArea()).rename('ndvi02_area')

    # adding area of vegetation as a band
    ndvi_img = ndvi_img.addBands(ndvi02_area)

    # calculate ndvi > 0.2 area
    ndviStats = ndvi02_area.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=10,
        maxPixels=1e29
    )

    image = image.set(ndviStats)

    # calculate area of AOI
    area = image.select('B1').multiply(0).add(1).multiply(ee.Image.pixelArea()).rename('area')

    # calculate area
    img_stats = area.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=geometry,
        scale=10,
        maxPixels=10 ** 13
    )
    image = image.set(img_stats)

    a = image.getNumber('ndvi02_area').divide(image.getNumber('area')).multiply(100)
    b = image.getNumber('ndvi02_area')

    rel_cover = image.select('B1').multiply(0).add(a).rename('rel_ndvi')
    image = image.addBands(rel_cover)
    image = image.addBands(ndvi)

    thres = ndvi.gte(0.2).rename('thres')
    image = image.addBands(thres)
    image = image.addBands(b)
    return image


def get_veg_stats(image):
    date = image.get('system:time_start')
    name = image.get('name')
    pixelArea = ee.Image.pixelArea()
    fixArea = 5961031705.843
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('ndvi')
    image = image.addBands(ndvi)

    ndvi02 = ndvi.gte(0.2).rename('ndvi02')
    image = image.addBands(ndvi02).updateMask(ndvi02)

    NDVIstats = image.select('ndvi02').reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=geometry,
        scale=10,
        maxPixels=1e29
    )
    NDVIarea = ee.Number(NDVIstats.get('ndvi02')).multiply(100)

    return ee.Feature(None, {
        'NDVIarea': NDVIarea,
        'name': name,
        'system:time_start': date})


# download image collection for the whole range of dates
collection = (ee.ImageCollection('COPERNICUS/S2')
              .filterDate(start_date, end_date)
              .filterBounds(geometry)
              .map(lambda image: image.clip(geometry))
              .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 1)))

latest_image = ee.Image(collection.toList(collection.size()).get(collection.size().subtract(1)))
first_image = ee.Image(collection.toList(collection.size()).get(0))
latest_image_date = latest_image.date().format("YYYY-MM-dd").getInfo()
first_image_date = first_image.date().format("YYYY-MM-dd").getInfo()
area_change = get_veg_stats(first_image).getInfo()["properties"]["NDVIarea"] - \
              get_veg_stats(latest_image).getInfo()["properties"]["NDVIarea"]
print(f'Change in vegetation area: {area_change}')
print(f'First image date: {first_image_date}\nLast image date: {latest_image_date}')

new_report = False
# compare date of latest image with last recorded image
# if there is new data it will set new_report to True
json_file_name = 'data.json'

with open(json_file_name, 'r', encoding='utf-8')as f:
    data = json.load(f)
    if timeframe not in data:
        print('Timeframe not yet covered. Will be generated.')
        new_report = True
        data[timeframe] = {'latest_image': latest_image_date,
                           'first_image': first_image_date,
                           'vegetation_area_change': area_change
                           }
    elif datetime.datetime.strptime(latest_image_date, '%Y-%m-%d') > datetime.datetime.strptime(data[timeframe]['latest_image'],'%Y-%m-%d'):
        print('New data. Updating File.')
        new_report = True
        data[timeframe] = {'latest_image': latest_image_date,
                           'first_image': first_image_date,
                           'vegetation_area_change': area_change
                           }
    else:
        print('No new data.')
        sys.exit()

with open(json_file_name, 'w', encoding='utf-8') as f:
    json.dump(data, f)

if new_report:
    # select images from collection
    ndvi_collection = collection.map(add_NDVI)
    # ndvi_collection = ndvi_collection.map(mask_cloud_and_shadows)
    ndvi_img_start = ee.Image(ndvi_collection.toList(ndvi_collection.size()).get(0))
    ndvi_img_end = ee.Image(ndvi_collection.toList(ndvi_collection.size()).get(ndvi_collection.size().subtract(1)))

    # calculate difference between the two datasets
    growth_decline_img = ndvi_img_end.select('thres').subtract(ndvi_img_start.select('thres'))
    growth_decline_img_mask = growth_decline_img.neq(0)
    growth_decline_img = growth_decline_img.updateMask(growth_decline_img_mask)
    cloud_vis_img = ndvi_img_end.select('QA60')



def add_ee_layer(self, ee_image_object, vis_params, name):
    """Adds a method for displaying Earth Engine image tiles to folium map."""
    map_id_dict = ee.Image(ee_image_object).getMapId(vis_params)
    folium.raster_layers.TileLayer(
        tiles=map_id_dict['tile_fetcher'].url_format,
        attr='Map Data &copy; <a href="https://earthengine.google.com/">Google Earth Engine</a>',
        name=name,
        overlay=True,
        control=True
    ).add_to(self)
# get the middle coordinate
def get_mean_coord(coords):
    first_val = [x[0] for x in coords]
    second_val = [x[1] for x in coords]
    return sum(first_val)/len(first_val), sum(second_val) / len(second_val)

growth_vis_params = {
    'palette': ['00FF00', 'FF0000']
}

# Add Earth Engine drawing method to folium.
folium.Map.add_ee_layer = add_ee_layer

basemaps = {
    'Google Maps': folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',
        attr='Google',
        name='Google Maps',
        overlay=True,
        control=True
    ),
    'Google Satellite': folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
        attr='Google',
        name='Google Satellite',
        overlay=True,
        control=True
    ),
    'Google Terrain': folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}',
        attr='Google',
        name='Google Terrain',
        overlay=True,
        control=True
    ),
    'Google Satellite Hybrid': folium.TileLayer(
        tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
        attr='Google',
        name='Google Satellite',
        overlay=True,
        control=True
    ),
    'Esri Satellite': folium.TileLayer(
        tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri',
        name='Esri Satellite',
        overlay=True,
        control=True
    )
}

# TODO: Add statistics
# swap out the coordinates because folium takes them the other way around
swapped_coords = [[x[1], x[0]] for x in geometry['coordinates'][0][0]]
# Define middle point of our map
lat, lon = get_mean_coord(swapped_coords)
my_map = folium.Map(location=[lat, lon], zoom_control=False)
basemaps['Google Satellite'].add_to(my_map)
my_map.add_ee_layer(growth_decline_img, growth_vis_params, 'Growth and decline image')

# fit bounds for optimal zoom level
my_map.fit_bounds(swapped_coords)
# add lines
folium.PolyLine(swapped_coords, color="white", weight=5, opacity=1).add_to(my_map)
my_map.save('map.html')
my_map

options = FirefoxOptions()
options.add_argument("--headless")
driver = selenium.webdriver.Firefox(options=options)
# for 300dpi a5 we need  2480x1748
driver.set_window_size(2480, 1748)  # choose a resolution
driver.get('file:///C:/Users/gilbe/PycharmProjects/NDVI-auto-processing/map.html')
driver.save_screenshot('growth_decline.jpg')

#generate pdf
pdf = FPDF(orientation='L')
pdf.add_page()
pdf.set_font("Arial", size=12)
pdf.cell(10, 10, txt=f'Vegetation area change: { area_change }')
pdf.image('growth_decline.jpg', x=10, y=20, w=IMAGE_WIDTH/10, h=IMAGE_HEIGHT/10)
pdf.output("report.pdf")

# TODO: remove clouds from calculation
# TODO: run analysis over the 4 data sets with the dynamic dates
# TODO: save output maps and stats to disk but discard raw data
# TODO: chart changes changes over time
# TODO: export as jpg. We need to generate a JPG map in a d good resolution.
# TODO: get scalebar
# desired output is: 300DPI, A5, basemap google satellite without labels, increase and decrease overlays.
