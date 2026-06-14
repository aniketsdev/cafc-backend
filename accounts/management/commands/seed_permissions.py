"""
Seed all permissions and role-permission mappings.

Usage:
    python manage.py seed_permissions          # Seed permissions + role mappings
    python manage.py seed_permissions --reset  # Delete existing and re-seed
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from accounts.models import Role, Permission, PermissionRoles


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------
ROLES = [
    {"name": "Admin", "type": "ADMIN", "description": "Full system access — all group homes, all residents, all features"},
    {"name": "Program Director", "type": "STAFF", "description": "Full system access — all group homes, all residents, all features"},
    {"name": "Program Manager", "type": "STAFF", "description": "Restricted access — view + limited edit, no critical data changes"},
    {"name": "BCBA", "type": "STAFF", "description": "Restricted access — view + limited edit, no critical data changes"},
    {"name": "Nurse", "type": "STAFF", "description": "Limited access — view all, edit specific documents only"},
    {"name": "Program Coordinator", "type": "STAFF", "description": "Restricted access — view + limited edit, no critical data changes"},
    {"name": "DSP", "type": "STAFF", "description": "Restricted access — view + limited edit, no critical data changes"},
    {"name": "Lead", "type": "LEAD", "description": None},
    {"name": "Agent", "type": "AGENT", "description": None},
    {"name": "Guardian", "type": "GUARDIAN", "description": None},
    {"name": "Resident", "type": "RESIDENT", "description": None},
]

# ---------------------------------------------------------------------------
# Permission definitions — module.action
# ---------------------------------------------------------------------------
PERMISSIONS = [
    # --- Lead Management ---
    {"module": "leads", "key": "view", "name": "View Leads List"},
    {"module": "leads", "key": "create", "name": "Add New Lead"},
    {"module": "leads", "key": "edit", "name": "Edit Lead Demographics"},
    {"module": "leads", "key": "delete", "name": "Reject Referral"},
    {"module": "leads", "key": "assign", "name": "View / Upload Intake Documents"},

    # --- Onboarding & Profile Management ---
    {"module": "onboarding", "key": "complete", "name": "Complete Onboarding"},
    # {"module": "onboarding", "key": "select_group_home", "name": "Select Group Home / Room"},
    # {"module": "onboarding", "key": "select_checkin_date", "name": "Select Check-in Date"},
    # {"module": "onboarding", "key": "mark_completed", "name": "Mark as Completed"},
    # {"module": "onboarding", "key": "send_guardian_credentials", "name": "Send Guardian Credentials"},
    # {"module": "onboarding", "key": "transfer_home", "name": "Transfer Resident to Another Home"},
    # ✅ ADD THESE (from sheet)
    {"module": "onboarding", "key": "move_out", "name": "Move Out Resident"},
    {"module": "onboarding", "key": "readmit", "name": "Re-Admit Resident"},
    {"module": "onboarding", "key": "transfer_home", "name": "Transfer Resident to Another Home"},

    # --- Document Management ---
    {"module": "documents", "key": "view", "name": "View Documents"},
    {"module": "documents", "key": "upload", "name": "Upload Document"},
    {"module": "documents", "key": "download", "name": "Download Document"},
    {"module": "documents", "key": "edit", "name": "Edit Document"},
    {"module": "documents", "key": "delete", "name": "Delete Document"},
    {"module": "documents", "key": "print", "name": "Print Document"},
    {"module": "documents", "key": "versioning", "name": "Document Versioning"},

    # --- Consent & Forms Management ---
    {"module": "consent_forms", "key": "view", "name": "View All Forms"},
    {"module": "consent_forms", "key": "create", "name": "Fill New Form"},
    {"module": "consent_forms", "key": "edit", "name": "Edit Cosent Form"},
    {"module": "consent_forms", "key": "print", "name": "Print Consent Form"},
    {"module": "consent_forms", "key": "share", "name": "Share Form with Guardian/Agent"},
    {"module": "consent_forms", "key": "delete", "name": "Delete Consent Form"},
    {"module": "consent_forms", "key": "sign", "name": "Sign Consent Form"},

    # --- ADLs (Activities of Daily Living) ---
    {"module": "adls", "key": "view", "name": "View ADL Data"},
    {"module": "adls", "key": "record", "name": "Record ADL Activities"},
    {"module": "adls", "key": "edit", "name": "Edit ADL Records"},

# --- Daily Tracking ---
    {"module": "daily_tracking", "key": "view", "name": "View Full Audit History"},
    

   

    # --- Monthly Summary ---
    {"module": "monthly_summary", "key": "view", "name": "View Monthly Summary"},
    {"module": "monthly_summary", "key": "generate_report", "name": "Generate Report"},

    # --- Goal / Activity Management ---
    {"module": "goals", "key": "view", "name": "View Goals"},
    {"module": "goals", "key": "create", "name": "Create Goal"},
    {"module": "goals", "key": "edit", "name": "Edit Goal"},
    {"module": "goals", "key": "delete", "name": "Delete Goal"},

    # --- Incident Management ---
    {"module": "incidents", "key": "view", "name": "View Incidents"},
    {"module": "incidents", "key": "create", "name": "Create New Incident"},
    {"module": "incidents", "key": "edit", "name": "Edit Incident"},
    {"module": "incidents", "key": "close", "name": "Close Incident"},
    {"module": "incidents", "key": "print_report", "name": "Print Incident Report"},
    {"module": "incidents", "key": "comment", "name": "Add Comment on Incident"},
    {"module": "incidents", "key": "start", "name": "Start Incident"},
    {"module": "incidents", "key": "submit_for_review", "name": "Submit Incident for PM Review"},
    {"module": "incidents", "key": "pm_review", "name": "PM Review and Sign-off"},
    {"module": "incidents", "key": "acknowledge", "name": "Acknowledge Incident (Portal)"},

    # --- Appointment Management ---
    {"module": "appointments", "key": "view", "name": "View Appointments"},
    {"module": "appointments", "key": "create", "name": "Create New Appointment"},
    {"module": "appointments", "key": "edit", "name": "Edit Appointment"},
    {"module": "appointments", "key": "mark_completed", "name": "Mark as Completed"},
    {"module": "appointments", "key": "delete", "name": "Delete Appointment"},

    # # --- Provider Management ---
    # {"module": "providers", "key": "view", "name": "View Providers"},
    # {"module": "providers", "key": "create", "name": "Create New Provider"},
    # {"module": "providers", "key": "edit", "name": "Edit Provider"},
    # {"module": "providers", "key": "delete", "name": "Delete Provider"},
    
    # --- Group Home Management ---
    {"module": "group_homes", "key": "view", "name": "View Group Homes List"},
    {"module": "group_homes", "key": "create", "name": "Add New Group Home"},
    {"module": "group_homes", "key": "edit", "name": "Edit Group Home Details"},
    {"module": "group_homes", "key": "view_profile", "name": "View Group Home Profile"},
    {"module": "group_homes", "key": "deactivate", "name": "Deactivate Group Home"},
    {"module": "group_homes", "key": "delete", "name": "Delete Group Home"},
    {"module": "group_homes", "key": "assign_staff", "name": "Assign Staff to Group Home"},


    # --- User Management ---
    {"module": "users", "key": "view", "name": "View Users List"},
    {"module": "users", "key": "create", "name": "Add New User "},
    {"module": "users", "key": "edit", "name": "Edit User Profile"},
    {"module": "users", "key": "deactivate", "name": "Deactivate User"},
    {"module": "users", "key": "role_assignment", "name": "Role Assignment"},
    {"module": "users", "key": "resend", "name": "Resend Invite Email"},
    {"module": "users", "key": "view and edit roles&permission", "name": "View Roles & Permissions Settings"},

    # --- Audit & System Logs ---
    # --- Audit & System Logs ---
#    {"module": "audit_logs", "key": "view_history", "name": "View Audit Log History"},
   #----usermanagement profile-----
   
  { "module": "profile", "key": "view_own_profile", "name": "View Own Profile", "scope": "ALL" },
  { "module": "profile", "key": "edit_own_profile", "name": "Edit Own Profile (Name, Phone, Photo)", "scope": "ALL" },
  { "module": "profile", "key": "change_own_password", "name": "Change Own Password", "scope": "ALL" },

    
]

# ---------------------------------------------------------------------------
# Full access roles — get ALL permissions with scope ALL
# ---------------------------------------------------------------------------
FULL_ACCESS_ROLE_NAMES = ["Admin"]

# ---------------------------------------------------------------------------
# Nurse permissions — limited
# Documents: only 5 & 30 day forms (view, download, edit, preview, print, versioning)
# No other module access
# ---------------------------------------------------------------------------
NURSE_PERMISSIONS = [
{"module": "consent_forms", "key": "create", "scope": "ALL", "form_types": ["5_day", "30_day"]},
{"module": "consent_forms", "key": "edit", "scope": "ALL", "form_types": ["5_day", "30_day"]},
{"module": "consent_forms", "key": "print", "scope": "ALL", "form_types": ["5_day", "30_day"]},
     # --- Lead permissions (same as BCBA) ---
    {"module": "leads", "key": "view", "scope": "ALL"},
    # {"module": "leads", "key": "assign", "scope": "ALL"},
    # ADLs permissions
    {"module": "adls", "key": "view", "scope": "ASSIGNED_HOME"},
    #-----view goals------
    {"module": "goals", "key": "view", "scope": "ASSIGNED_HOME"},
    #---------monthly summary------
    {"module": "monthly_summary", "key": "view","scope" : "ASSIGNED_HOME"},
    #--------Incident----
    {"module": "incidents", "key": "view", "scope" : "ASSIGNED_HOME"},
    {"module": "incidents", "key": "create", "scope" : "ASSIGNED_HOME"},
    {"module": "incidents", "key": "edit", "scope" : "ASSIGNED_HOME"},
    {"module": "incidents", "key": "print_report", "scope" : "ASSIGNED_HOME"},
    {"module": "incidents", "key": "comment", "scope" : "ASSIGNED_HOME"},
    #----appointment management----
    {"module": "appointments", "key": "view", "scope" : "ASSIGNED_HOME"},
    {"module": "appointments", "key": "create", "scope" : "ASSIGNED_HOME"},
    {"module": "appointments", "key": "edit", "scope" : "ASSIGNED_HOME"},
    {"module": "appointments", "key": "mark_completed", "scope" : "ASSIGNED_HOME"},
    # #----provider management----
    # {"module": "providers", "key": "view", "scope": "ASSIGNED_HOME"},
    # {"module": "providers", "key": "create", "scope": "ASSIGNED_HOME"},
    # {"module": "providers", "key": "edit", "scope": "ASSIGNED_HOME"},
    # {"module": "providers", "key": "delete", "scope": "ASSIGNED_HOME"},
    #--------group home----
    {"module": "group_homes", "key": "view", "scope" : "ASSIGNED_HOME"},
    {"module": "group_homes", "key": "view_profile", "scope" : "ASSIGNED_HOME"},
    #-------profile management-----
    { "module": "profile", "key": "view_own_profile","scope": "ALL" },
    { "module": "profile", "key": "edit_own_profile","scope": "ALL" },
    { "module": "profile", "key": "change_own_password","scope": "ALL" },

    


]

# ---------------------------------------------------------------------------
# Program Coordinator — scoped to assigned group home
# All permissions but scoped to ASSIGNED_HOME
# Except: group_homes.create, group_homes.deactivate, group_homes.delete,
#          users.*, audit_logs (except view_house_logs)
# ---------------------------------------------------------------------------
COORDINATOR_EXCLUDED = [
    # Group home management — create/deactivate/delete are system-wide only
    ("group_homes", "create"),
    ("group_homes", "deactivate"),
    ("group_homes", "delete"),
    ("group_homes", "assign_staff"),
    # Incident — acknowledge is portal-only
    ("incidents", "acknowledge"),
    # User management — view-only: can see users but NOT create/edit/deactivate/assign roles
    ("users", "create"),
    ("users", "edit"),
    ("users", "deactivate"),
    ("users", "role_assignment"),
    # Audit logs — only house logs allowed (added separately)
    ("daily_tracking", "view"),
   
    # Leads — not allowed
  
    ("leads", "create"),
    ("leads", "edit"),
    ("leads", "delete"),
   #onboarding
    ("onboarding","move_out"),
    ("onboarding","readmit"),
    ("onboarding","transfer_home"),
  #-----audit logs---
    ("daily_tracking", "view"),
 # --- User Management ---

    ("users", "create"),
    ("users", "edit"),
    ("users", "deactivate"),
    ("users", "role_assignment"),
    ("users", "resend"),
    ("users", "view and edit roles&permission"),
   

]
PROGRAM_DIRECTOR_EXCLUDED = [
    ("group_homes", "delete"),
]
BCBA_EXCLUDED = [
    ("leads", "create"),
    ("leads", "edit"),
    ("leads", "delete"),  # assuming reject referral maps to this
    ("onboarding","move_out"),
    ("onboarding","readmit"),
    ("onboarding","transfer_home"),
    ("onboarding","complete"),
    ("documents", "delete"),
    # for consent and forms
    ("consent_forms","delete"),
    ("consent_forms","sign"),
    #----appointment manangement
    ("appointments","Delete Appointment"),
    #---group home----
     ("group_homes","create"),
     ("group_homes","edit"),
     ("group_homes","deactivate"),

     ("group_homes","delete"),
     ("group_homes","assign_staff"),
     ("daily_tracking", "view"),
    # --- User Management ---
("users", "view"),
("users", "create"),
("users", "edit"),
("users", "deactivate"),
("users", "role_assignment"),
("users", "resend"),
("users", "view and edit roles&permission"),
#------audit logs----
("daily_tracking", "view"),
# --- New incident workflow perms not granted to BCBA ---
("incidents", "start"),
("incidents", "submit_for_review"),
("incidents", "pm_review"),
("incidents", "acknowledge"),



]
PM_EXCLUDED = [
    ("onboarding", "transfer_home"),
    ("consent_forms","sign"),

]

# ---------------------------------------------------------------------------
# DSP — restricted access
# View + limited edit; no critical data changes
# ---------------------------------------------------------------------------
DSP_PERMISSIONS = [
    # ADLs — view, record, edit (scoped)
    {"module": "adls", "key": "view", "scope": "ASSIGNED_HOME"},
    {"module": "adls", "key": "record", "scope": "ASSIGNED_HOME"},
    {"module": "adls", "key": "edit", "scope": "ASSIGNED_HOME"},

   

    # Goals — view, track progress (scoped), edit limited
    {"module": "goals", "key": "view", "scope": "ASSIGNED_HOME"},

    # Incidents — view, create, edit (own DRAFT + IN_PROGRESS), print, comment,
    # and the new workflow transitions (start / submit_for_review) — all scoped.
    # `incidents.edit` is required for the row-level "Edit" action and for the
    # incident detail drawer that hosts the Sign & Start button.
    {"module": "incidents", "key": "view", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "create", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "edit", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "print_report", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "comment", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "start", "scope": "ASSIGNED_HOME"},
    {"module": "incidents", "key": "submit_for_review", "scope": "ASSIGNED_HOME"},


    # Appointments — view, filter, mark completed (scoped) — NO edit, no delete
    {"module": "appointments", "key": "view", "scope": "ASSIGNED_HOME"},
    {"module": "appointments", "key": "mark_completed", "scope": "ASSIGNED_HOME"},
    # # Providers — view only (scoped)
    # {"module": "providers", "key": "view", "scope": "ASSIGNED_HOME"},

    # Group homes — view, view profile (scoped)
    {"module": "group_homes", "key": "view", "scope": "ASSIGNED_HOME"},
    {"module": "group_homes", "key": "view_profile", "scope": "ASSIGNED_HOME"},

    # Audit logs — view house logs only (scoped)
    #document permission 
    {"module": "documents", "key": "view", "scope": "ALL"},
    #cosent and forms
    {"module": "consent_forms", "key": "view", "scope": "ALL"},
    #--------------profile management 
    { "module": "profile", "key": "view_own_profile","scope": "ALL" },
    { "module": "profile", "key": "edit_own_profile","scope": "ALL" },
    { "module": "profile", "key": "change_own_password","scope": "ALL" },

    


]
CONSENT_FORM_PERMISSIONS = [
    {"module": "consent_forms", "key": "view", "scope": "ALL"},
    {"module": "consent_forms", "key": "create", "scope": "ALL"},
    {"module": "consent_forms", "key": "edit", "scope": "ALL"},
    {"module": "consent_forms", "key": "print", "scope": "ALL"},
    {"module": "consent_forms", "key": "share", "scope": "ALL"},
    {"module": "consent_forms", "key": "view_history", "scope": "ALL"},
    {"module": "consent_forms", "key": "delete", "scope": "ALL"},

   
]


class Command(BaseCommand):
    help = "Seed permissions and role-permission mappings based on the access matrix"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing permissions and role mappings before seeding",
        )

    def handle(self, *args, **options):
        reset = options["reset"]

        with transaction.atomic():
            if reset:
                self.stdout.write("Deleting existing permission mappings...")
                PermissionRoles.objects.all().delete()
                Permission.objects.all().delete()
                self.stdout.write(self.style.WARNING("Deleted all permissions and mappings."))

            # --- 1. Create / update roles (keyed by unique name) ---
            role_map = {}
            for role_def in ROLES:
                role, created = Role.objects.update_or_create(
                    name=role_def["name"],
                    defaults={
                        "type": role_def["type"],
                        "description": role_def["description"],
                    },
                )
                role_map[role_def["name"]] = role
                action = "Created" if created else "Updated"
                self.stdout.write(f"  {action} role: {role.name} ({role.type})")
                

            # --- 2. Create / update permissions ---
            perm_map = {}
            for perm_def in PERMISSIONS:
                perm, created = Permission.objects.update_or_create(
                    module=perm_def["module"],
                    key=perm_def["key"],
                    defaults={
                        "name": perm_def["name"],
                    },
                )
                perm_map[(perm_def["module"], perm_def["key"])] = perm
                if created:
                    self.stdout.write(f"  Created permission: {perm.module}.{perm.key}")
                # --- Program Director (ALL except restricted ones) ---
            pd_role = role_map["Program Director"]
            pd_count = 0

            for perm_key, perm in perm_map.items():
                if perm_key in PROGRAM_DIRECTOR_EXCLUDED:
                    continue  # ❌ NO

                PermissionRoles.objects.update_or_create(
                   role=pd_role,
                   permission=perm,
                   defaults={"scope": PermissionRoles.SCOPE_ALL},
                )
                pd_count += 1

            self.stdout.write(f"  Mapped {pd_count} permissions to Program Director (restricted)") 

            # --- 3. Map full-access roles (Admin, PD, PM, BCBA) ---
            for role_name in FULL_ACCESS_ROLE_NAMES:
                role = role_map[role_name]
                for perm in perm_map.values():
                    PermissionRoles.objects.update_or_create(
                        role=role,
                        permission=perm,
                        defaults={"scope": PermissionRoles.SCOPE_ALL},
                    )
                self.stdout.write(f"  Mapped {len(perm_map)} permissions to {role.name} (ALL)")

            # --- 4. Map Nurse permissions ---
            nurse_role = role_map["Nurse"]
            nurse_count = 0
            for nurse_perm in NURSE_PERMISSIONS:
                perm_key = (nurse_perm["module"], nurse_perm["key"])
                if perm_key in perm_map:
                    PermissionRoles.objects.update_or_create(
                        role=nurse_role,
                        permission=perm_map[perm_key],
                        defaults={
                            "scope": nurse_perm["scope"],
                            "display": (
                                "5 & 30 Day Forms Only"
                                if nurse_perm["module"] == "consent_forms"
                                else (
                                    "Assigned Home"
                                    if nurse_perm["scope"] == PermissionRoles.SCOPE_ASSIGNED_HOME
                                    else "Yes"
                                )
                            )
                        }
                    )
                    nurse_count += 1
            self.stdout.write(f"  Mapped {nurse_count} permissions to Nurse")
                     # --- BCBA permissions (all except restricted ones) ---
            bcba_role = role_map["BCBA"]
            bcba_count = 0

            for perm_key, perm in perm_map.items():
                if perm_key in BCBA_EXCLUDED:
                   continue

                PermissionRoles.objects.update_or_create(
                role=bcba_role,
                permission=perm,
                defaults={"scope": PermissionRoles.SCOPE_ALL},
                )
                bcba_count += 1

            self.stdout.write(f"  Mapped {bcba_count} permissions to BCBA (restricted)")
            # --- 5. Map Program Coordinator permissions ---
            coord_role = role_map["Program Coordinator"]
            coord_count = 0

            # Define the permissions you want Program Coordinator to have full access ("Yes")
            FULL_ACCESS_PERMS_FOR_COORDINATOR = [
              # NOTE: ("leads","view") and ("users","view") are deliberately NOT
              # listed here. Per this file's header (see lines ~202-206), Program
              # Coordinator is scoped to its ASSIGNED_HOME for everything. Granting
              # those two as scope=ALL leaked the entire org-wide leads/users lists
              # (RBAC regression: Coordinator saw the same total_records as Admin).
              # Omitting them lets the loop below fall through to ASSIGNED_HOME.
              ("leads", "assign"),
              ("profile","view_own_profile"),
              ("profile","edit_own_profile"),
              ("profile","change_own_password"),

            # add other module.key tuples where full access is needed
]

            for perm_key, perm in perm_map.items():
              if perm_key in COORDINATOR_EXCLUDED:
                continue

              if perm_key in FULL_ACCESS_PERMS_FOR_COORDINATOR:
                  scope = PermissionRoles.SCOPE_ALL  # full access for these permissions
              else:
                  scope = PermissionRoles.SCOPE_ASSIGNED_HOME  # assigned home for others

              PermissionRoles.objects.update_or_create(
              role=coord_role,
              permission=perm,
              defaults={"scope": scope},
              )
              coord_count += 1

            self.stdout.write(f"  Mapped {coord_count} permissions to Program Coordinator (mixed scopes)")
            # --- 5b. Map Program Manager permissions (YES/NO only, no ASSIGNED_HOME) ---
            # --- Program Manager (as per sheet EXACT rules) ---
            pm_role = role_map["Program Manager"]
            pm_count = 0

            PM_ALLOWED = [
                # --- Leads ---
                ("leads", "view"),
                ("leads", "create"),
                ("leads", "edit"),
                ("leads", "delete"),
                ("leads", "assign"),

                # --- Onboarding ---
                ("onboarding", "complete"),
                ("onboarding", "move_out"),
                ("onboarding", "readmit"),
                # document management 
                ("documents", "view"),
                ("documents", "upload"),
                ("documents", "download"),
                ("documents", "edit"),
                ("documents", "delete"),
                ("documents", "preview"),
                ("documents", "print"),
                ("documents", "versioning"),
                ("adls","view"),
                ("adls","record"),
                ("adls","edit"),
                #----goals------
                ("goals","view"),
                ("goals","edit"),
                ("goals","create"),
                ("goals","delete"),
                #------monthly summary---
                ("monthly_summary","view"),
                ("monthly_summary","generate_report"),
                #------Incident----
                ("incidents","view"),
                ("incidents","create"),
                ("incidents","edit"),
                ("incidents","close"),
                ("incidents","print_report"),
                ("incidents","comment"),
                ("incidents","start"),
                ("incidents","submit_for_review"),
                ("incidents","pm_review"),
                # ❌ NOT including transfer_home
                #----appointment management---
                ("appointments","view"),
                ("appointments","create"),
                ("appointments","edit"),
                ("appointments","mark_completed"),
                ("appointments","delete"),
                # #----provider management---
                # ("providers","view"),
                # ("providers","create"),
                # ("providers","edit"),
                # ("providers","delete"),
                #-----Audit and system logs----
                ("daily_tracking","view"),
                #-----Group Home Management-----
                ("group_homes","view"),
                ("group_homes","edit"),
                ("group_homes","view_profile"),
                #-------audit logs----
                # --- User Management ---
                ("users", "view"),
                ("users", "create"),
                ("users", "edit"),
                ("users", "deactivate"),
                ("users", "role_assignment"),
                ("users", "resend"),
                ("users", "view and edit roles&permission"),
                #-----profile management----
                ("profile","view_own_profile"),
                ("profile","edit_own_profile"),
                ("profile","change_own_password"),

   

            ]

            for perm_key, perm in perm_map.items():

                if perm_key not in PM_ALLOWED:
                   continue  # ❌ NO

                PermissionRoles.objects.update_or_create(
                    role=pm_role,
                    permission=perm,
                    defaults={"scope": PermissionRoles.SCOPE_ALL},  # ✅ ONLY YES
                )
                pm_count += 1

            self.stdout.write(f"  Mapped {pm_count} permissions to Program Manager (sheet-based)")
            # --- 6. Map DSP permissions ---
            dsp_role = role_map["DSP"]
            dsp_count = 0
            for dsp_perm in DSP_PERMISSIONS:
                perm_key = (dsp_perm["module"], dsp_perm["key"])
                if perm_key in perm_map:
                    PermissionRoles.objects.update_or_create(
                        role=dsp_role,
                        permission=perm_map[perm_key],
                        defaults={"scope": dsp_perm["scope"]},
                    )
                    dsp_count += 1
            self.stdout.write(f"  Mapped {dsp_count} permissions to DSP")
            # --- 7. FIX Consent Forms (override scopes) ---
            for role_name in ["Admin", "Program Director","Program Manager"]:
                role = role_map[role_name]

                # First REMOVE existing consent mappings
                PermissionRoles.objects.filter(
                role=role,
                permission__module="consent_forms"
                ).delete()

                # Re-add based on config
                for perm_def in CONSENT_FORM_PERMISSIONS:
                    perm_key = (perm_def["module"], perm_def["key"])

                    if perm_key in perm_map:
                       PermissionRoles.objects.update_or_create(
                          role=role,
                          permission=perm_map[perm_key],
                          defaults={"scope": perm_def["scope"]},
                        )

        total_mappings = PermissionRoles.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone! {len(ROLES)} roles, {len(PERMISSIONS)} permissions, {total_mappings} role-permission mappings."
            )
        )
