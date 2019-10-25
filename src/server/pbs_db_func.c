/*
 * Copyright (C) 1994-2021 Altair Engineering, Inc.
 * For more information, contact Altair at www.altair.com.
 *
 * This file is part of both the OpenPBS software ("OpenPBS")
 * and the PBS Professional ("PBS Pro") software.
 *
 * Open Source License Information:
 *
 * OpenPBS is free software. You can redistribute it and/or modify it under
 * the terms of the GNU Affero General Public License as published by the
 * Free Software Foundation, either version 3 of the License, or (at your
 * option) any later version.
 *
 * OpenPBS is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public
 * License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 * Commercial License Information:
 *
 * PBS Pro is commercially licensed software that shares a common core with
 * the OpenPBS software.  For a copy of the commercial license terms and
 * conditions, go to: (http://www.pbspro.com/agreement.html) or contact the
 * Altair Legal Department.
 *
 * Altair's dual-license business model allows companies, individuals, and
 * organizations to create proprietary derivative works of OpenPBS and
 * distribute them - whether embedded or bundled with other software -
 * under a commercial license agreement.
 *
 * Use of Altair's trademarks, including but not limited to "PBS™",
 * "OpenPBS®", "PBS Professional®", and "PBS Pro™" and Altair's logos is
 * subject to Altair's trademark licensing policies.
 */


/**
 * @file    pbsd_db_func.c
 *
 * @brief
 * pbsd_db_func.c - contains functions to initialize several pbs data structures.
 *
 */
#include <pbs_config.h>   /* the master config generated by configure */

#include <sys/types.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <memory.h>
#include <signal.h>
#include <time.h>
#include <sys/stat.h>
#include <libutil.h>

#include <dirent.h>
#include <grp.h>
#include <netdb.h>
#include <pwd.h>
#include <unistd.h>
#include <sys/param.h>
#include <sys/resource.h>
#include <sys/time.h>

#include "libpbs.h"
#include "pbs_ifl.h"
#include "net_connect.h"
#include "log.h"
#include "list_link.h"
#include "attribute.h"
#include "server_limits.h"
#include "server.h"
#include "credential.h"
#include "ticket.h"
#include "batch_request.h"
#include "work_task.h"
#include "resv_node.h"
#include "job.h"
#include "queue.h"
#include "reservation.h"
#include "pbs_db.h"
#include "pbs_nodes.h"
#include "tracking.h"
#include "provision.h"
#include "avltree.h"
#include "svrfunc.h"
#include "acct.h"
#include "pbs_version.h"
#include "pbs_license.h"
#include "resource.h"
#include "pbs_python.h"
#include "hook.h"
#include "hook_func.h"
#include "pbs_share.h"
#include "pbs_undolr.h"

#ifndef SIGKILL
/* there is some weid stuff in gcc include files signal.h & sys/params.h */
#include <signal.h>
#endif

#define MAX_DB_RETRIES			5
#define MAX_DB_LOOP_DELAY		10
#define IPV4_STR_LEN	15
static int db_oper_failed_times = 0;
static int last_rc = -1; /* we need to reset db_oper_failed_times for each state change of the db */
static int conn_db_state = 0;
extern int pbs_failover_active;
extern int server_init_type;
extern int stalone;	/* is program running not as a service ? */
char conn_db_host[PBS_MAXSERVERNAME+1];	/* db host where connection is made */
void *svr_db_conn = NULL; /* server's global database connection pointer */
void *conn = NULL;  /* pointer to work out a valid connection - later assigned to svr_db_conn */
extern int pbs_decrypt_pwd(char *, int, size_t, char **, const unsigned char *, const unsigned char *);
extern pid_t go_to_background();
void *setup_db_connection(char *, int , int );
static void *get_db_connect_information();
static int touch_db_stop_file(void);
static int start_db();
void stop_db();

/**
 * @brief
 *		Checks whether database is down, and if so
 *		starts up the database in asynchronous mode.
 *
 * @return - Failure code
 * @retval   PBS_DB_DOWN - Error in pbs_start_db
 * @retval   PBS_DB_STARTING - Database is starting
 *
 */
static int
start_db()
{
	char *failstr = NULL;
	int rc;

	log_eventf(PBSEVENT_SYSTEM | PBSEVENT_FORCE, PBS_EVENTCLASS_SERVER, LOG_CRIT, msg_daemonname, "Starting PBS dataservice");

	rc = pbs_start_db(conn_db_host, pbs_conf.pbs_data_service_port);
	if (rc != 0) {
		if (rc == PBS_DB_OOM_ERR) {
			pbs_db_get_errmsg(PBS_DB_OOM_ERR, &failstr);
			snprintf(log_buffer, LOG_BUF_SIZE, "%s %s", "WARNING:", failstr ? failstr : "");
		} else {
			pbs_db_get_errmsg(PBS_DB_ERR, &failstr);
			snprintf(log_buffer, LOG_BUF_SIZE, "%s %s", "Failed to start PBS dataservice.", failstr ? failstr : "");
		}
		log_eventf(PBSEVENT_SYSTEM | PBSEVENT_ADMIN, PBS_EVENTCLASS_SERVER, LOG_ERR, msg_daemonname, log_buffer);
		fprintf(stderr, "%s\n", log_buffer);
		free(failstr);
		if (rc != PBS_DB_OOM_ERR)
			return PBS_DB_DOWN;
	}

	sleep(1); /* give time for database to atleast establish the ports */
	return PBS_DB_STARTING;
}

/**
 * @brief
 *		Stop the database if up, and log a message if the database
 *		failed to stop.
 *		Try to stop till not successful, with incremental delay.
 */
void
stop_db()
{
	char *db_err = NULL;
	int db_delay = 0;
	pbs_db_disconnect(svr_db_conn);
	svr_db_conn = NULL;

	/* check status of db, shutdown if up */
	db_oper_failed_times = 0;
	while (1) {
		if (pbs_status_db(conn_db_host, pbs_conf.pbs_data_service_port) != 0)
			return; /* dataservice not running, got killed? */

		log_eventf(PBSEVENT_SYSTEM | PBSEVENT_FORCE, PBS_EVENTCLASS_SERVER, LOG_CRIT, msg_daemonname, "Stopping PBS dataservice");

		if (pbs_stop_db(conn_db_host, pbs_conf.pbs_data_service_port) != 0) {
			pbs_db_get_errmsg(PBS_DB_ERR, &db_err);
			snprintf(log_buffer, LOG_BUF_SIZE, "%s %s", "Failed to stop PBS dataservice.", db_err ? db_err : "");
			log_eventf( PBSEVENT_SYSTEM | PBSEVENT_ADMIN, PBS_EVENTCLASS_SERVER, LOG_ERR, msg_daemonname, log_buffer);
			fprintf(stderr, "%s\n", log_buffer);
			free(db_err);
			db_err = NULL;
		}

		db_oper_failed_times++;
		/* try stopping after some time again */
		db_delay = (int)(1 + db_oper_failed_times * 1.5);
		if (db_oper_failed_times > MAX_DB_LOOP_DELAY)
			db_delay = MAX_DB_LOOP_DELAY; /* limit to MAX_DB_LOOP_DELAY secs */
		sleep(db_delay); /* don't burn the CPU looping too fast */
		return;
	}
}

/**
 * @brief
 *	Attempt to mail a message to "mail_from" (administrator), shut down
 *	the database, close the log and exit the Server. Called when a database save fails.
 *	Panic shutdown of server due to database error. Closing database and log system.
 *
 */
void
panic_stop_db()
{
	char panic_stop_txt[] = "Panic shutdown of Server on database error.  Please check PBS_HOME file system for no space condition.";

	log_err(-1, __func__, panic_stop_txt);
	svr_mailowner(0, 0, 0, panic_stop_txt);
	stop_db();
	log_close(1);
	exit(1);
}

/**
 * @brief
 *		Setup a new database connection structure.
 *
 * @par Functionality:
 *		Disconnect and destroy any active connection associated
 *		with global variable conn.
 *		It then calls pbs_db_connect to initialize a new connection
 *		structure (pointed to by conn) with the values of host, timeout and
 *		have_db_control. Logs error on failure.
 *
 * @param[in]	host	- The host to connect to
 * @param[in]	port	- The port to connect to
 * @param[in]	timeout	- The connection timeout
 *
 * @return	Initialized connection handle.
 * @retval  !NULL - Connection Established.
 * @retval  NULL - No Connection.
 *
 */
void *
setup_db_connection(char *host, int port, int timeout)
{
	int failcode = 0;
	int rc = 0;
	char *conn_db_err = NULL;
	void *lconn = NULL;

	/* Make sure we have the database instance up and running */
	/* If the services are down, retry will attempt to start_db() */
	rc = pbs_status_db(host, port);
	if (rc == 1)
		return NULL;
	else if (rc == -1)
		failcode = PBS_DB_ERR;
	else
		failcode = pbs_db_connect(&lconn, host, port, timeout);
	if (!lconn) {
		pbs_db_get_errmsg(failcode, &conn_db_err);
		if (conn_db_err) {
			log_event(PBSEVENT_SYSTEM | PBSEVENT_FORCE, PBS_EVENTCLASS_SERVER,
					LOG_CRIT, msg_daemonname, conn_db_err);
			free(conn_db_err);
		}
	}
	return lconn;
}

/**
 * @brief
 *		This function creates connection information which is used by
 * 		connect_to_db.
 *
 * @return - void pointer
 * @retval  NULL - Function failed. Error will be logged
 * @retval  !NULL - Newly allocated connection handler.
 *
 */
static void *
get_db_connect_information()
{
	void *lconn = NULL;
	int rc = 0;
	int conn_timeout = 0;
	char *failstr = NULL;

	/*
	 * Decide where to connect to, the timeout, and whether we can have control over the
	 * database instance or not. Based on these create a new connection structure by calling
	 * setup_db_connection. The behavior is as follows:
	 *
	 * a) If external database is configured (pbs_data_service_host), then always connect to that
	 *	and do not try to start/stop the database (both in standalone / failover cases). In case of a
	 *	connection failure (in between pbs processing) in standalone setup, try reconnecting to the
	 *	external database for ever.
	 *	In case of connection failure (not at startup) in a failover setup, try connecting to the external
	 *	database only once more and then quit, letting failover kick in.
	 *
	 *
	 * b) With embedded database:
	 *	Status the database:
	 *	- If no database running, start database locally.
	 *
	 *	- If database already running locally, its all good.
	 *
	 *	- If database is running on another host, then,
	 *		a) If standalone, continue to attempt to start database locally.
	 *		b) If primary, attempt to connect to secondary db, if it
	 *		   connects, then throw error and start over (since primary
	 *		   should never use the secondary's database. If connect fails
	 *		   database is then try to start database locally.
	 *		c) If secondary, attempt connection to primary db; if it
	 *		   connects, continue to use it happily. If it fails, attempt to start
	 *		   database locally.
	 *
	 */
	if (pbs_conf.pbs_data_service_host) {
		/*
		 * External database configured,  infinite timeout, database instance not in our control
		 */
		conn_timeout = PBS_DB_CNT_TIMEOUT_INFINITE;
		strncpy(conn_db_host, pbs_conf.pbs_data_service_host, PBS_MAXSERVERNAME);
	} else {
		/*
		 * Database is in our control, we need to figure out the status of the database first
		 * Is it already running? Is it running on another machine?
		 *  Check whether database is up or down.
		 *  It calls pbs_status_db to figure out the database status.
		 *  pbs_db_status returns:
		 *	-1 - failed to execute
		 *	0  - Data service running on local host
		 *	1  - Data service NOT running
		 *	2  - Data service running on another host
		 *
		 * If pbs_db_status is not sure whether db is running or not, then it attempts
		 * to connect to the host database to confirm that.
		 *
		 */
		if (pbs_conf.pbs_primary) {
			if (!pbs_failover_active)
				strncpy(conn_db_host, pbs_conf.pbs_primary, PBS_MAXSERVERNAME);
			else
				strncpy(conn_db_host, pbs_conf.pbs_secondary, PBS_MAXSERVERNAME);
		} else
			strncpy(conn_db_host, pbs_default(), PBS_MAXSERVERNAME); /* connect to pbs.server */

		rc = pbs_status_db(conn_db_host, pbs_conf.pbs_data_service_port);
		if (rc == -1) {
			pbs_db_get_errmsg(PBS_DB_ERR, &failstr);
			log_errf(PBSE_INTERNAL, msg_daemonname, "status db failed: %s", failstr ? failstr : "");
			free(failstr);
			return NULL;
		}

		log_eventf(PBSEVENT_SYSTEM, PBS_EVENTCLASS_SERVER, LOG_INFO, msg_daemonname, "pbs_status_db exit code %d", rc);

		if (last_rc != rc) {
			/*
			 * we might have failed trying to start database several times locally
			 * if however, the database state has changed (like its stopped by admin),
			 * then we reset db_oper_failed_times.
			 *
			 * Basically we check against the error code from the last try, if its
			 * not the same error code, then it means that something in the database
			 * startup has changed (or failing to start for a different reason).
			 * Since the db_oper_failed_times is used to count the number of failures
			 * of one particular kind, so we reset it when the error code differs
			 * from that in the last try.
			 */
			db_oper_failed_times = 0;
		}
		last_rc = rc;

		if (pbs_conf.pbs_primary) {
			if (rc == 0 || rc == 1) /* db running locally or db not running */
				conn_timeout = PBS_DB_CNT_TIMEOUT_INFINITE;
			if (rc == 2) /* db could be running on secondary, don't start, try connecting to secondary's */
				conn_timeout = PBS_DB_CNT_TIMEOUT_NORMAL;

			if (!pbs_failover_active) {
				/* Failover is configured, and this is the primary */
				if (rc == 0 || rc == 1) /* db running locally or db not running */
					strncpy(conn_db_host, pbs_conf.pbs_primary, PBS_MAXSERVERNAME);

				if (rc == 2) /* db could be running on secondary, don't start, try connecting to secondary's */
					strncpy(conn_db_host, pbs_conf.pbs_secondary, PBS_MAXSERVERNAME);

			} else {
				/* Failover is configured and this is active secondary */
				if (rc == 0 || rc == 1) /* db running locally or db not running */
					strncpy(conn_db_host, pbs_conf.pbs_secondary, PBS_MAXSERVERNAME);

				/* db could be running on primary, don't start, try connecting to primary's */
				if (rc == 2) {
					strncpy(conn_db_host, pbs_conf.pbs_primary, PBS_MAXSERVERNAME);
					conn_db_state = PBS_DB_STARTED;
				}
			}
		} else {
			/*
			 * No failover configured. Try connecting forever to our own instance, have control.
			 */
			conn_timeout = PBS_DB_CNT_TIMEOUT_INFINITE;
		}
	}
	if (rc == 1)
		conn_db_state = start_db();

	if(conn_db_state == PBS_DB_STARTING || conn_db_state == PBS_DB_STARTED)
		lconn = setup_db_connection(conn_db_host, pbs_conf.pbs_data_service_port, conn_timeout);
	return lconn;
}

/**
 * @brief
 * 		touch_db_stop_file	- create a touch file when db is stopped.
 *
 * @return	int
 * @retval	0	- created touch file
 * @retval	-1	- unable to create touch file
 */
static int
touch_db_stop_file(void)
{
	int fd;
	char closefile[MAXPATHLEN + 1];
	snprintf(closefile, MAXPATHLEN, "%s/datastore/pbs_dbclose", pbs_conf.pbs_home_path);

#ifndef O_RSYNC
#define O_RSYNC 0
#endif
	if ((fd = open(closefile, O_WRONLY| O_CREAT | O_RSYNC, 0600)) != -1)
		return -1;
	close(fd);
	return 0;
}

/**
 * @brief
 * 		connect_to_db	- Try and continue forever till a successful database connection is made.
 *
 * @param[in]	background	- Process can attempt to connect in the background.
 * @return	int
 * @retval	0	- Success
 * @retval	Non-Zero	- Failure
 */
int
connect_to_db(int background) {
	int try_db = 0;
	int db_stop_counts = 0;
	int db_stop_email_sent = 0;
	int conn_state;
#ifndef DEBUG
	pid_t sid = -1;
#endif
	int db_delay = 0;
try_db_again:
	fprintf(stdout, "Connecting to PBS dataservice.");

	conn_state = PBS_DB_CONNECT_STATE_NOT_CONNECTED;
	db_oper_failed_times = 0;

	while (1) {
#ifndef DEBUG
		fprintf(stdout, ".");
#endif
		if (conn_state == PBS_DB_CONNECT_STATE_FAILED) {
			pbs_db_disconnect(conn);
			conn_state = PBS_DB_CONNECT_STATE_NOT_CONNECTED;
			db_oper_failed_times++;
			conn_db_state = PBS_DB_DOWN; /* allow to retry to start db again */
		} else if (conn_state == PBS_DB_CONNECT_STATE_CONNECTED) {
			sprintf(log_buffer, "connected to PBS dataservice@%s", conn_db_host);
			log_event(PBSEVENT_SYSTEM | PBSEVENT_FORCE,
				PBS_EVENTCLASS_SERVER, LOG_CRIT,
				msg_daemonname, log_buffer);
			fprintf(stdout, "%s\n", log_buffer);
		}

		if (conn && conn_state == PBS_DB_CONNECT_STATE_CONNECTED)
			break;

		if (conn_db_state == PBS_DB_DOWN) {
			conn_db_state = start_db();
			if (conn_db_state == PBS_DB_STARTING) {
				/* started new database instance, reset connection */
				pbs_db_disconnect(conn); /* disconnect from any old connection, cleanup memory */
				conn_state = PBS_DB_CONNECT_STATE_NOT_CONNECTED;
			} else if (conn_db_state == PBS_DB_DOWN) {
				db_oper_failed_times++;
			}
		}

		if (conn_state == PBS_DB_CONNECT_STATE_NOT_CONNECTED) {
			/* get fresh connection */
			if ((conn = get_db_connect_information()) == NULL)
				conn_state = PBS_DB_CONNECT_STATE_FAILED;
			else
				conn_state = PBS_DB_CONNECT_STATE_CONNECTED;
		}
		db_delay = (int)(1 + db_oper_failed_times * 1.5);
		if (db_delay > MAX_DB_LOOP_DELAY)
			db_delay = MAX_DB_LOOP_DELAY; /* limit to MAX_DB_LOOP_DELAY secs */
		sleep(db_delay);     /* dont burn the CPU looping too fast */
		update_svrlive();    /* indicate we are alive */
#ifndef DEBUG
		if (background && try_db >= 4) {
			fprintf(stdout, "continuing in background.\n");
			if ((sid = go_to_background()) == -1)
				return (2);
		}
#endif	/* DEBUG is defined */
		try_db ++;
	}

	if (!pbs_conf.pbs_data_service_host) {
		/*
		 * Check the connected host and see if it is connected to right host.
		 * In case of a failover, PBS server should be connected to database
		 * on the same host as it is executing on. Thus, if PBS server ends
		 * up connected to a database on another host (say primary server
		 * connected to database on secondary or vice versa), then it is
		 * deemed unacceptable. In such a case throw error on log notifying
		 * that PBS is attempting to stop the database on the other side
		 * and restart the loop all over.
		 */
		if (pbs_conf.pbs_primary) {
			if (!pbs_failover_active) {
				/* primary instance */
				if (strcmp(conn_db_host, pbs_conf.pbs_primary) != 0) {
					/* primary instance connected to secondary database, not acceptable */
					log_errf(-1, msg_daemonname, "PBS data service is up on the secondary instance, attempting to stop it");
					pbs_db_disconnect(conn);
					conn = NULL;

					touch_db_stop_file();

					if (db_stop_email_sent == 0) {
						if (++db_stop_counts > MAX_DB_RETRIES) {
							log_errf(-1, msg_daemonname, "Not able to stop PBS data service at the secondary site, please stop manually");
							svr_mailowner(0, 0, 1, log_buffer);
							db_stop_email_sent = 1;
						}
					}
					sleep(10);
					goto try_db_again;
				}
			} else {
				/* secondary instance */
				if (strcmp(conn_db_host, pbs_conf.pbs_primary) == 0) {
					/* secondary instance connected to primary database, not acceptable */
					log_errf(-1, msg_daemonname, "PBS data service is up on the primary instance, attempting to stop it");

					pbs_db_disconnect(conn);
					conn = NULL;

					touch_db_stop_file();

					if (db_stop_email_sent == 0) {
						if (++db_stop_counts > MAX_DB_RETRIES) {
							log_errf(-1, msg_daemonname, "Not able to stop PBS data service at the primary site, please stop manually");
							svr_mailowner(0, 0, 1, log_buffer);
							db_stop_email_sent = 1;
						}
					}
					sleep(10);
					goto try_db_again;
				}
			}
		}
	}

	svr_db_conn = conn; /* use this connection */
	conn = NULL; /* ensure conn does not point to svr_db_conn any more */
	return 0;
}

/**
 * @brief
 *	Frees attribute list memory
 *
 * @param[in]	attr_list - List of pbs_db_attr_list_t objects
 *
 * @return      None
 *
 */
void
free_db_attr_list(pbs_db_attr_list_t *attr_list)
{
	if (attr_list->attr_count > 0) {
		free_attrlist(&attr_list->attrs);
		attr_list->attr_count = 0;
	}
}


