"""Spatial dimensions for rendered 2D arrays and HWC color images."""
def image_hw(shape):
    if len(shape) == 3 and shape[-1] in (1, 3, 4):
        return int(shape[0]), int(shape[1])
    return int(shape[-2]), int(shape[-1])


def clip_polygon(coords, width, height):
    """Sutherland–Hodgman clipping against the image rectangle."""
    points = list(zip(coords[0::2], coords[1::2]))
    for axis, bound, lower in ((0, 0, True), (0, width, False), (1, 0, True), (1, height, False)):
        output = []
        if not points:
            break
        prev = points[-1]
        prev_in = prev[axis] >= bound if lower else prev[axis] <= bound
        for cur in points:
            cur_in = cur[axis] >= bound if lower else cur[axis] <= bound
            if cur_in != prev_in:
                t = (bound-prev[axis])/(cur[axis]-prev[axis])
                output.append(tuple(prev[i]+t*(cur[i]-prev[i]) for i in (0,1)))
            if cur_in:
                output.append(cur)
            prev, prev_in = cur, cur_in
        points = output
    unique = []
    for point in points:
        if not unique or point != unique[-1]:
            unique.append(point)
    if len(unique)>1 and unique[-1]==unique[0]: unique.pop()
    return [value for point in unique for value in point] if len(unique)>=3 else []
