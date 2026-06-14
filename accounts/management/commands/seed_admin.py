"""
Seed a default Admin user for login.

Usage:
    python manage.py seed_admin
    python manage.py seed_admin --email admin@mailinator.com --password Test@123
    python manage.py seed_admin --reset   # Reset password if user already exists
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Role, User


DEFAULT_EMAIL = "admin@mailinator.com"
DEFAULT_PASSWORD = "Test@123"
DEFAULT_USERNAME = "admin"
DEFAULT_FIRST_NAME = "Super"
DEFAULT_LAST_NAME = "Admin"


class Command(BaseCommand):
    help = "Create a default Admin user for login (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=DEFAULT_EMAIL, help="Admin email")
        parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Admin password")
        parser.add_argument("--username", default=DEFAULT_USERNAME, help="Admin username")
        parser.add_argument("--first-name", default=DEFAULT_FIRST_NAME)
        parser.add_argument("--last-name", default=DEFAULT_LAST_NAME)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="If the admin user already exists, reset its password and ensure Admin role.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        password = options["password"]
        username = options["username"]
        first_name = options["first_name"]
        last_name = options["last_name"]
        reset = options["reset"]

        with transaction.atomic():
            admin_role, role_created = Role.objects.get_or_create(
                name="Admin",
                defaults={
                    "type": "ADMIN",
                    "description": "Full system access — all group homes, all residents, all features",
                },
            )
            if role_created:
                self.stdout.write(self.style.WARNING(
                    "Admin role did not exist — created it. "
                    "Run `python manage.py seed_permissions` to attach permissions."
                ))

            user = User.objects.filter(email=email).first()
            if user:
                if reset:
                    user.set_password(password)
                    user.role = admin_role
                    user.active = True
                    user.is_superuser = True
                    user.email_verified_at = user.email_verified_at or timezone.now()
                    user.save()
                    self.stdout.write(self.style.SUCCESS(
                        f"Admin user '{email}' already existed — password reset and role re-applied."
                    ))
                else:
                    self.stdout.write(self.style.WARNING(
                        f"Admin user '{email}' already exists. "
                        f"Re-run with --reset to update password."
                    ))
                return

            user = User.objects.create_user(
                email=email,
                password=password,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=admin_role,
                active=True,
                is_superuser=True,
                email_verified_at=timezone.now(),
            )

            self.stdout.write(self.style.SUCCESS(
                f"\nDone! Admin user created.\n"
                f"  Email:    {user.email}\n"
                f"  Username: {user.username}\n"
                f"  Password: {password}\n"
                f"  Role:     {admin_role.name}\n"
            ))
