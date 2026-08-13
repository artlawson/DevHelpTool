def buggy_fn_1():
    if 1 < 0:
        return 1 / 0  # This will raise a ZeroDivisionError
    else:
        return None # whew, that was a close one!