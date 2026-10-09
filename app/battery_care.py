"""Tips for keeping the battery healthy, from its settings and recent history.

The Solarbank 4 uses LFP cells, which wear mostly from time spent at the
extremes (full or empty), from heat, and from cycling. The Modbus interface
doesn't report cell temperature or state of health, so those are general
advice rather than readings.
"""

from __future__ import annotations


def care(snapshot: dict, soc: dict, control: dict | None = None) -> dict:
    rated = snapshot.get("rated_kwh")
    discharged = snapshot.get("discharged_total_kwh")
    cycles = round(discharged / rated, 1) if rated and discharged is not None else None
    tips: list[dict] = []

    def tip(level: str, title: str, text: str) -> None:
        tips.append({"level": level, "title": title, "text": text})

    top = snapshot.get("charging_limit_soc")
    bottom = snapshot.get("discharge_limit_soc")
    full, empty = soc.get("full_share"), soc.get("empty_share")
    enough = soc.get("days", 0) >= 3

    if top is not None and top >= 100 and enough and full is not None and full > 0.25:
        tip("suggest", "Often sitting full",
            f"The battery was at 98% or more for {full:.0%} of the last {min(30, round(soc['days']))} days. "
            "Setting the charge limit to 90–95% in the Anker app keeps it out of the top of its range, "
            "at the cost of a little capacity.")
    elif top is not None and top >= 100:
        tip("info", "Charge limit is 100%",
            "That's fine if the battery empties most evenings. If it often stays full for hours, 90–95% is gentler.")
    elif top is not None:
        tip("good", f"Charge limit {top:.0f}%", "Stopping short of full reduces wear on the cells.")

    if bottom is not None and bottom < 5:
        tip("suggest", f"Discharge limit {bottom:.0f}%",
            "Letting it run completely flat is the hardest use for the cells. A limit of 5–10% in the Anker app "
            "keeps a small reserve.")
    elif enough and empty is not None and empty > 0.25:
        tip("suggest", "Often sitting empty",
            f"The battery was at 7% or less for {empty:.0%} of the time. That usually means it's smaller than the "
            "evening's use; holding it in cheap hours or charging it overnight keeps it out of the bottom of its range.")

    if control and control.get("grid_charge") and control.get("charge_target_soc", 0) >= 100:
        tip("suggest", "Grid charging to 100%",
            "Charging from the grid to 90% leaves room for morning solar and avoids sitting full overnight.")

    if cycles is not None:
        tip("info", f"About {cycles:g} full cycles so far",
            "Worked out from the total energy discharged and the rated capacity. LFP batteries are built for "
            "thousands of cycles, so one a day is normal use.")

    tip("info", "Temperature",
        "The battery doesn't report its temperature over Modbus. Keep it out of direct sun and frost; cold "
        "slows charging and heat ages the cells fastest.")
    return {"cycles": cycles, "soc": soc, "tips": tips}
