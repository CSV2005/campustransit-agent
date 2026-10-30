# Fares, Tickets and Passes — What Costs What

## BMTC ordinary bus fare slabs (revised 5 January 2025)
BMTC fares are charged by fare stage, not by route. The published revised slabs for
ordinary non-AC services are:

| Distance | Adult fare |
|---|---|
| First 2 km | Rs 6 |
| Up to 4 km | Rs 12 |
| Up to 6 km | Rs 18 |
| 8-10 km | Rs 23 |
| 12-14 km | Rs 24 |
| 16-18 km | Rs 28 |
| 20-40 km | Rs 30 |
| Up to 50 km | Rs 32 (maximum ordinary fare) |

Senior citizens pay a concessional fare (Rs 5 for the first 2 km, Rs 24 at 50 km).
Children pay about half. The fare tables in the GTFS feed carry their own
stage-to-stage prices, and those take precedence when a stage pair is present.

## BMTC pass prices (revised 9 January 2025)
| Pass | Price | Notes |
|---|---|---|
| Daily pass (ordinary) | Rs 80 | Up from Rs 70 |
| Weekly pass (ordinary) | Rs 350 | Up from Rs 300 |
| Monthly pass (ordinary) | Rs 1,200 | Up from Rs 1,050 |
| Ordinary extension monthly pass | Rs 1,500 | For newly extended routes beyond 40 km |
| Vajra AC daily pass | Rs 140 | Up from Rs 120 |
| Vajra AC monthly pass | Rs 2,000 | Up from Rs 1,800 |
| Vayu Vajra Gold monthly pass | Rs 4,000 | Includes airport services, toll and GST |
| Student monthly pass (AC) | Rs 1,400 | Up from Rs 1,200 |
| Student pass (non-AC) | Free since 12 June 2026 | State free student pass scheme |

## Namma Metro fares
BMRCL charges distance-based single-journey fares. In the GTFS feed the token
products range from Rs 10 for a short hop to Rs 90 for the longest journey across
the network. Fares are higher at peak hours for some products. There is no student
monthly pass in the published BMRCL fare products, so a student commute that mixes
metro and bus pays the metro leg in full even when the bus leg is free.

Metro lines, as of the 2026-27 GTFS feed:
- Green Line: Madavara to Silk Institute, 32 stations.
- Purple Line: Whitefield (Kadugodi) to Challaghatta.
- Yellow Line: Rashtreeya Vidyalaya Road to Delta Electronics Bommasandra.

Interchanges matter for routing: Majestic connects Green and Purple, and RV Road
connects Green and Yellow.

## What a student actually pays in 2026-27
Three rules decide almost every case:

1. A girl student who is a Karnataka resident pays nothing on non-AC BMTC and KSRTC
   buses, under Shakti. She may still need a metro ticket or an AC pass.
2. Any other student pays nothing on non-AC BMTC and KSRTC services under the free
   student pass, once the pass is issued. Until the card is issued, they pay normal
   fares.
3. Anyone using AC services pays the AC fare or buys the AC pass: Rs 1,400 per month
   for students, Rs 2,000 for other commuters, Rs 4,000 for the Vayu Vajra Gold pass.

## Worked example: Jayanagar to DSU Harohalli
The trip is roughly 38 km by road. Route 213-V runs Harohalli to Sanjay Gandhi
Hospital through Jayanagar, so it is a one-seat ride for many students along the
corridor. At the published ordinary slab, a 38 km trip falls in the 20-40 km band,
so the single fare is about Rs 30. A return trip is about Rs 60. Over 24 travel
days that is about Rs 1,440 a month.

Against that: the free student pass costs nothing for a non-AC commuter, so it saves
the full Rs 1,440. The AC student pass at Rs 1,400 would only break even if the
student actually used AC buses, and route 213-V is a non-AC ordinary service.
A non-student commuter compares Rs 1,440 against the Rs 1,200 ordinary monthly
pass, which saves about Rs 240.

## Common fare questions
- Toll charges are not included in BMTC passes. Buses using NICE Road tolls charge
  the toll separately.
- Vayu Vajra airport fares are much higher than city fares and are not covered by
  ordinary or Vajra passes.
- Fare amounts change by government notification, usually with a 15 per cent
  revision. Any quoted fare should carry the date it was last verified.
