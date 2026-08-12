def buggy_fn_2():
    return 318 + [1,2,3] # will raise a TypeError because you cannot add an integer and a list