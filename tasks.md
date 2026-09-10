# Pending tasks

## Instructions

When starting these tasks, first branch off a new branch. These can be long and intricate changes that might
end up not being wanted. Each session should have its own branch so as to not pollute the main branch which
is a known "good" state.  

Each item should be at minimum one commit. Never mix tasks in a single commit. Tasks can span multiple commits
if the work is large enough.  

Before starting any work, investigate each task and clear up any questions ahead of time. The idea is to only
need user interaction at the beginning of a session and at the end of the session. The session should be
as unattended as possible.

As tasks are done, prefix the existing entry here with a [done]. If a task is unable to be completed and is
blocked for any reason, leave it and present the reasons why it was not done at the end of the session.  

When the tasks are addressed, make sure the branch is committed and up to date, but not pushed, and a local
staging environment should be setup. Present the changes done in the session and the URL for the staging
deployment.  

## Task list

- [done] When a food is entered and no carbs/cals are entered or known at the time of entry, the food should be added
  to a "pending update" list. This should be a list of foods that are awaiting details and can be addressed
  like a todo list. When a pending food gets required details entered, it moves to the saved foods list and
  meals referencing it should have their details updated. These pending foods can appear at the top of the
  'Foods' table.

- [done] The desktop meal response table should show dose required to cover the meal and the IOB at the start of the
  meal. Currently if food is entered without carbs, no reference is given at all, even if other carbs are
  entered. This is just unnecessary obfuscation and the system should just display the dose for the known
  carbs.

- [done] The meal response table should be moved up above the Daily intake table. Rename 'IOB @ Start' to 'IOB'.
  Remove the Ref note. Remove the note beside the table title. Do this for all tables. Thier purpose is easily
  understood.

- [done] On the phone side of the app, the graph should show the smoothed trend like the desktop view. Also remove
  the axis labels but add a label somewhere showing how many hours the graph is showing, maybe along the
  bottom.

- [done] For the meal entry on the phone, move the meal name entry to just above the meal type selection. I find i
  dont use saved meals as often and it should be a secondary focus.

- [done] The log entries on the phone app should allow me to long press on them to edit/delete them. The long press
  should pop up an edit dialog with a 'Save' and 'Delete' button.

- [done] The bolus calculator does not need the long disclaimer on it any more. It should show stacked values. The
  current correction (and its target), the calculated bolus for the entered meal, and the currently active
  IOB.

- [done] Enter pending food details from the phone, without adding weight to the
  interface: a self-hiding chip on the Meal tab opens a sheet of the foods
  awaiting carbs, editable and savable there.
