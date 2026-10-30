[out:json][timeout:180];
(
  node["amenity"~"university|college"](12.30,77.20,13.15,77.80);
  way["amenity"~"university|college"](12.30,77.20,13.15,77.80);
  node["railway"="station"]["station"="subway"](12.30,77.20,13.15,77.80);
  node["place"~"suburb|neighbourhood|quarter|town|village"](12.30,77.20,13.15,77.80);
  node["amenity"="bus_station"](12.30,77.20,13.15,77.80);
);
out center tags;
