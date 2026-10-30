# Namma Metro and Multi-Modal Commutes

## What the metro covers
Three lines carry passengers today:

- **Green Line** — Madavara to Silk Institute, 32 stations. This is the line that
  matters most for the Kanakapura Road corridor: Silk Institute is its southern
  terminus and the closest metro station to Harohalli.
- **Purple Line** — Whitefield (Kadugodi) to Challaghatta, running east-west through
  Majestic.
- **Yellow Line** — Rashtreeya Vidyalaya Road to Delta Electronics Bommasandra,
  serving the Bommasandra industrial belt.

Majestic is the Green-Purple interchange. RV Road is the Green-Yellow interchange.

A fourth line, the **Pink Line** (Kalena Agrahara to Nagawara), is built but **not yet
carrying passengers**. Its 7.5 km elevated section from Kalena Agrahara to Tavarekere
(6 stations) completed the final CMRS safety inspection on 8 October 2026 and is
awaiting authorisation, with services expected in late October or early November 2026.
The 13.76 km underground section from Dairy Circle to Nagawara (12 stations) is
targeted for early 2027. The full line is 21.26 km with 18 stations. When it opens it
will interchange with the Yellow Line at Jayadeva Hospital and with the Purple Line at
MG Road, and will give Bannerghatta Road, JP Nagar, Shivajinagar and Nagawara a direct
north-south metro spine. Until then, no journey should be planned on it.

## Metro fares and ticketing
Metro fares range from Rs 10 for a short hop to Rs 90 for the longest journey across
the network, in Rs 10 steps. In the BMRCL feed the fare between any two stations is
determined entirely by how many stations the journey spans: 1-2 stops Rs 10, 3-4 Rs 20,
5-6 Rs 30, 7-8 Rs 40, 9-10 Rs 50, 11-15 Rs 60, 16-20 Rs 70, 21-25 Rs 80, and 26 or more
stops Rs 90. Tickets are bought as tokens, on a travel card, or through the Namma Metro
app with a QR ticket. A 5 per cent fare revision announced for 9 February 2026 was kept
in abeyance and has not been implemented, so the Rs 10-90 table remains in force.

There is no student monthly pass among BMRCL's published fare products. The state
free student pass scheme covers the four road transport corporations, not the metro,
so the metro leg of a student commute is always paid. A student who travels Silk
Institute to Majestic pays the metro fare for that leg even if the bus legs on
either side are free.

## Timings
Metro services typically start around 05:00 and run until about 23:00, with
slightly later services on weekends. The GTFS feed carries per-trip timings, but
BMRCL does not publish per-stop times for intermediate stations; the feed's
intermediate times are estimated from stop spacing and average speed and can be a
couple of minutes out.

## Combining bus and metro
The useful pattern for a long commute is: feeder bus or short BMTC ride to the
nearest metro station, metro across the city, then a bus from the metro station
nearest the destination. For the Harohalli corridor the practical version is:

1. BMTC or KSRTC ordinary bus from the student's locality to Silk Institute metro
   station, which sits at 12.8616 N, 77.5299 E.
2. Green Line north to Majestic if the destination is central.
3. A BMTC bus from Majestic to the final destination.

This only beats a single-seat bus when the destination is near a metro line and the
bus route is congested. For a DSU Harohalli student whose home is on the Kanakapura
Road side, route 213-V straight through is usually simpler than bus-metro-bus, and
it is free under the student pass.

## Metro access details
BMRCL's GTFS includes station entrances, pathways and platform levels. Stations with
multiple entrances can save a lot of walking if you pick the right one: for example,
Majestic has separate entrances for the Green and Purple platforms. The pathway data
carries traversal times, which matter when making a tight interchange.

## Auto and last-mile
Autos are outside the scope of this dataset. Auto fares in Bengaluru were last
revised separately from bus fares and are metered. For a short last-mile hop from a
metro station, walking to the nearest bus stop is usually cheaper than an auto, and
the free student pass covers the bus.
