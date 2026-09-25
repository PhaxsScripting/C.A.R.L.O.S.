from recovery import GiggleBudget


def test_repeated_crashes_stop_even_if_each_process_survived_a_minute():
    budget = GiggleBudget()
    assert budget.retry_after(70) == 2
    assert budget.retry_after(150) == 4
    assert budget.retry_after(230) is None
    assert budget.retry_after(900) is None


def test_separated_failures_expire_without_accumulating_false_block():
    budget = GiggleBudget()
    assert budget.retry_after(0) == 2
    assert budget.retry_after(700) == 2
    assert budget.retry_after(1400) == 2
