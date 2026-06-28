import copy
from tqdm import tqdm


# encode_historical_speeds is now replaced by encode_speeds_dct from utils
def _fill_missing_time_slots_temporal(way_time_speeds, neighbor_size):
    TIMESLOTS = range(2016)                 # 0 to 2015
    SLOTS_PER_DAY = 288

    # Considering the neighboring timeslots
    neighbor_size = neighbor_size
    for edge_id in tqdm(way_time_speeds):
        time_dict = copy.deepcopy(way_time_speeds[edge_id])
        for t in TIMESLOTS:
            if t not in time_dict:
                start = max(0, t - neighbor_size)
                end = min(2015, t + neighbor_size)
                all_speeds = []
                for s in range(start, end + 1):
                    if s in time_dict:
                        all_speeds.extend(time_dict[s])
                if all_speeds:
                    avg = sum(all_speeds) / len(all_speeds)
                    time_dict[t] = [avg]
        way_time_speeds[edge_id] = time_dict
    
    return way_time_speeds