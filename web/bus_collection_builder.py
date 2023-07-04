from collections import defaultdict
from itertools import repeat
from math import radians
from typing import Generator, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.neighbors import BallTree

from config import BUS_COLLECTION_SEARCH_AREA
from models.fetch_relation import (FetchRelationBusStop,
                                   FetchRelationBusStopCollection,
                                   PublicTransport)
from utils import haversine_distance, radians_tuple


def _pick_best(elements: list[FetchRelationBusStop]) -> tuple[Sequence[FetchRelationBusStop], Sequence[FetchRelationBusStop]]:
    if not elements:
        return tuple(), tuple()

    elements_explicit = tuple(e for e in elements if e.highway == 'bus_stop')
    elements_implicit = tuple(e for e in elements if e.highway != 'bus_stop')

    return elements_explicit, elements_implicit


def _assign(primary: Sequence[FetchRelationBusStop], elements: Sequence[FetchRelationBusStop], *, element_reuse: bool) -> Generator[FetchRelationBusStop | None, None, None]:
    if len(elements) >= 2:
        # find the closest stop to each platform
        if len(elements) < len(primary):
            # disallow reuse of elements
            if not element_reuse:
                return repeat(None, len(primary))

            tree = BallTree(tuple(radians_tuple(e.latLng) for e in elements), metric='haversine')
            query_indices = tree.query(
                tuple(radians_tuple(p.latLng) for p in primary),
                k=1,
                return_distance=False,
                sort_results=False)

            return (elements[i] for i in query_indices[:, 0])

        # minimize the total distance between each platform and stop
        else:
            distance_matrix = np.zeros((len(primary), len(elements)))

            # compute the haversine distance between each platform and stop
            for i, p in enumerate(primary):
                for j, e in enumerate(elements):
                    distance_matrix[i, j] = haversine_distance(p.latLng, e.latLng)

            # use the Hungarian algorithm to find the optimal assignment
            row_ind, col_ind = linear_sum_assignment(distance_matrix)

            # ensure the assignments are sorted by platform indices
            assignments = sorted(zip(row_ind, col_ind))

            # get the assigned stop for each platform
            return (elements[j] for _, j in assignments)

    elif len(elements) == 1:
        # disallow reuse of elements
        if not element_reuse and len(primary) > 1:
            return repeat(None, len(primary))

        return repeat(elements[0], len(primary))
    else:
        return repeat(None, len(primary))


def build_bus_stop_collections(bus_stops: list[FetchRelationBusStop]) -> list[FetchRelationBusStopCollection]:
    # 1. group by area
    # 2. group by name in area
    # 3. discard unnamed if in area with named
    # 4. for each named group, pick best platform and best stop

    if not bus_stops:
        return []

    search_latLng = BUS_COLLECTION_SEARCH_AREA / 111_111
    search_latLng_rad = radians(search_latLng)

    bus_stops_coordinates = tuple(radians_tuple(bus_stop.latLng) for bus_stop in bus_stops)
    bus_stops_tree = BallTree(bus_stops_coordinates, metric='haversine')

    areas: dict[int, int] = {}

    query_indices, _ = bus_stops_tree.query_radius(
        bus_stops_coordinates,
        r=search_latLng_rad,
        return_distance=True,
        sort_results=True)

    # group by area
    for i, indices in enumerate(query_indices):
        for j in indices[1:]:
            if (j_in := areas.get(j)) is not None:
                areas[i] = j_in
                break
        else:
            areas[i] = i

    area_groups: dict[int, list[FetchRelationBusStop]] = defaultdict(list)

    for member_index, area_index in areas.items():
        area_groups[area_index].append(bus_stops[member_index])

    collections: list[FetchRelationBusStopCollection] = []

    for area_group in area_groups.values():
        # group by name in area
        name_groups: dict[str, list[FetchRelationBusStop]] = defaultdict(list)
        for bus_stop in area_group:
            name_groups[bus_stop.groupName].append(bus_stop)

        # discard unnamed if in area with named
        if len(name_groups) > 1:
            name_groups.pop('', None)

        # expand non-number suffixed groups to number suffixed groups if needed
        prefix_map = defaultdict(list)

        for name_group_key, name_group in name_groups.items():
            parts = name_group_key.split(' ')

            if len(parts) > 1 and parts[-1].isdecimal():
                parts.pop()
                prefix_map[' '.join(parts)].append(name_group_key)

        for prefix, name_group_keys in prefix_map.items():
            if (prefix_name_group := name_groups.get(prefix)) is None:
                continue

            success = False

            for name_group_key in name_group_keys:
                name_group = name_groups[name_group_key]

                for prefix_bus_stop in prefix_name_group:
                    if not any(
                            bus_stop.public_transport == prefix_bus_stop.public_transport
                            for bus_stop in name_group):
                        name_group.append(prefix_bus_stop)
                        success = True

            if success:
                name_groups.pop(prefix)

        # for each named group, pick best platform and best stop
        for name_group_key, name_group in name_groups.items():
            platforms: list[FetchRelationBusStop] = []
            stops: list[FetchRelationBusStop] = []

            for bus_stop in name_group:
                if bus_stop.public_transport == PublicTransport.PLATFORM:
                    platforms.append(bus_stop)
                elif bus_stop.public_transport == PublicTransport.STOP_POSITION:
                    stops.append(bus_stop)
                else:
                    raise NotImplementedError(f'Unknown public transport type: {bus_stop.public_transport}')

            # for deterministic results
            platforms.sort(key=lambda p: p.id)
            stops.sort(key=lambda s: s.id)

            platforms_explicit, platforms_implicit = _pick_best(platforms)
            stops_explicit, stops_implicit = _pick_best(stops)

            if platforms_explicit and stops_explicit:
                print(f'🚧 Warning: Unexpected explicit platforms and stops for {name_group_key}')

            if platforms_explicit:
                for platform, stop in zip(platforms_explicit, _assign(platforms_explicit, stops, element_reuse=True)):
                    collections.append(FetchRelationBusStopCollection(
                        platform=platform,
                        stop=stop))

                continue

            if stops_explicit:
                for stop, platform in zip(stops_explicit, _assign(stops_explicit, platforms, element_reuse=False)):
                    collections.append(FetchRelationBusStopCollection(
                        platform=platform,
                        stop=stop))

                continue

            if platforms_implicit and stops_implicit:
                for platform, stop in zip(platforms_implicit, _assign(platforms_implicit, stops, element_reuse=True)):
                    collections.append(FetchRelationBusStopCollection(
                        platform=platform,
                        stop=stop))

                continue

            if platforms_implicit:  # and not stops_implicit
                for platform in platforms_implicit:
                    collections.append(FetchRelationBusStopCollection(
                        platform=platform,
                        stop=None))

                continue

            if stops_implicit:  # and not platforms_implicit
                for stop in stops_implicit:
                    collections.append(FetchRelationBusStopCollection(
                        platform=None,
                        stop=stop))

                continue

    return collections