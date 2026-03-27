def roles(request):
    if not request.user.is_authenticated:
        return {}
    g = set(request.user.groups.values_list("name", flat=True))
    return {
        "is_trader": "traders" in g,
        "is_compliance": "compliance_approver" in g,
        "is_middleoffice": "middleoffice_approver" in g,
    }
