"""
Shared factory_boy model factories for all tests.

Use these instead of raw model.objects.create() calls so that:
  - Test data is realistic and consistent
  - Default values are always valid
  - Relationships are automatically wired
"""

import factory
from factory.django import DjangoModelFactory

from apps.tenants.models import Organization, OrganizationMembership, RoleEnum
from apps.users.models import User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Faker("name")
    is_verified = True
    is_active = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        manager = cls._get_manager(model_class)
        return manager.create_user(*args, **kwargs)


class OrganizationFactory(DjangoModelFactory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Test Org {n}")
    slug = factory.LazyAttribute(lambda o: o.name.lower().replace(" ", "-"))
    plan = "FREE"
    is_active = True


class MembershipFactory(DjangoModelFactory):
    class Meta:
        model = OrganizationMembership

    organization = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    role = RoleEnum.MEMBER
