"""答辩演示用的社团报名业务模块。"""

registrations = []


def register(name, student_id):
    """登记学生；初始版本故意缺少重复学号校验，供 Agent 修复演示。"""
    if not name or not student_id:
        raise ValueError("姓名和学号不能为空")
    record = {"name": name.strip(), "student_id": student_id.strip()}
    registrations.append(record)
    return record


def list_registrations():
    return list(registrations)
