/*
 * Copyright (C) 1994-2021 Altair Engineering, Inc.
 * For more information, contact Altair at www.altair.com.
 *
 * This file is part of the PBS Professional ("PBS Pro") software.
 *
 * Open Source License Information:
 *
 * PBS Pro is free software. You can redistribute it and/or modify it under the
 * terms of the GNU Affero General Public License as published by the Free
 * Software Foundation, either version 3 of the License, or (at your option) any
 * later version.
 *
 * PBS Pro is distributed in the hope that it will be useful, but WITHOUT ANY
 * WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 * FOR A PARTICULAR PURPOSE.
 * See the GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 * Commercial License Information:
 *
 * For a copy of the commercial license terms and conditions,
 * go to: (http://www.pbspro.com/UserArea/agreement.html)
 * or contact the Altair Legal Department.
 *
 * Altair’s dual-license business model allows companies, individuals, and
 * organizations to create proprietary derivative works of PBS Pro and
 * distribute them - whether embedded or bundled with other software -
 * under a commercial license agreement.
 *
 * Use of Altair’s trademarks, including but not limited to "PBS™",
 * "PBS Professional®", and "PBS Pro™" and Altair’s logos is subject to Altair's
 * trademark licensing policies.
 *
 */


/**
 * @file    db_aerospike_common.c
 *
 * @brief
 *      This file contains aerospike specific implementation of functions
 *	to access the PBS aerospike database.
 *	This is aerospike specific data store implementation, and should not be
 *	used directly by the rest of the PBS code.
 *
 */

#include <pbs_config.h>   /* the master config generated by configure */
#include "pbs_db.h"
#include "db_aerospike.h"
#include <sys/wait.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <arpa/inet.h>
#include "ticket.h"


#define IPV4_STR_LEN	15

char *errmsg_cache = NULL;

extern int pbs_decrypt_pwd(char *, int, size_t, char **, const unsigned char *, const unsigned char *);
extern unsigned char pbs_aes_key[][16];
extern unsigned char pbs_aes_iv[][16];

char g_namespace[20] = "pbs";

aerospike as;


/**
 * An array of structures(of function pointers) for each of the database object
 */
pbs_db_fn_t db_fn_arr[PBS_DB_NUM_TYPES] = {
	{	/* PBS_DB_SVR */
		pbs_db_save_svr,
		NULL,
		pbs_db_load_svr,
		NULL,
		pbs_db_del_attr_svr,
		NULL
	},
	{	/* PBS_DB_SCHED */
		pbs_db_save_sched,
		pbs_db_delete_sched,
		pbs_db_load_sched,
		pbs_db_find_sched,
		pbs_db_del_attr_sched,
		pbs_db_reset_sched
	},
	{	/* PBS_DB_QUE */
		pbs_db_save_que,
		pbs_db_delete_que,
		pbs_db_load_que,
		pbs_db_find_que,
		pbs_db_del_attr_que,
		pbs_db_reset_que
	},
	{	/* PBS_DB_NODE */
		pbs_db_save_node,
		pbs_db_delete_node,
		pbs_db_load_node,
		pbs_db_find_node,
		pbs_db_del_attr_node,
		NULL
	},
	{	/* PBS_DB_MOMINFO_TIME */
		pbs_db_save_mominfo_tm,
		NULL,
		pbs_db_load_mominfo_tm,
		NULL,
		NULL,
		NULL
	},
	{	/* PBS_DB_JOB */
		pbs_db_save_job,
		pbs_db_delete_job,
		pbs_db_load_job,
		pbs_db_find_job,
		pbs_db_del_attr_job,
		pbs_db_reset_job
	},
	{	/* PBS_DB_JOBSCR */
		pbs_db_save_jobscr,
		NULL,
		pbs_db_load_jobscr,
		NULL,
		NULL,
		NULL
	},
	{	/* PBS_DB_RESV */
		pbs_db_save_resv,
		pbs_db_delete_resv,
		pbs_db_load_resv,
		pbs_db_find_resv,
		pbs_db_del_attr_resv,
		pbs_db_reset_resv
	}
};


long db_time_now(int i)
{
	long time_msec;
	struct timeval tval;

	gettimeofday(&tval, NULL);
	time_msec = (tval.tv_sec * 1000L) + (tval.tv_usec / 1000L);
	return time_msec;
}

/**
 * @brief
 *      Initialize a query state variable, before being used in a cursor
 *
 * @param[in]   conn - Database connection handle
 *
 * @return      Pointer to opaque cursor state handle
 * @retval      NULL - Failure to allocate memory
 * @retval      !NULL - Success - returns the new state variable
 *
 */
static void *
db_initialize_state(void *conn, query_cb_t query_cb)
{
	db_query_state_t *state = malloc(sizeof(db_query_state_t));
	if (!state)
		return NULL;
	state->count = 0;
	state->row = 0;
	state->query_cb = query_cb;
	return state;
}

/**
 * @brief
 *      Destroy a query state variable.
 *      Clears the database resultset and free's the memory allocated to
 *      the state variable
 *
 * @param[in]   st - Pointer to the state variable
 *
 */
static void
db_destroy_state(void *st)
{
	db_query_state_t *state = st;
	if (state) {
		free(state);
	}
}

/**
 * @brief
 *	Search the database for exisitn objects and load the server structures.
 *
 * @param[in]	conn - Connected database handle
 * @param[in]	pbs_db_obj_info_t - The pointer to the wrapper object which
 *		describes the PBS object (job/resv/node etc) that is wrapped
 *		inside it.
 * @param[in/out]	pbs_db_query_options_t - Pointer to the options object that can
 *		contain the flags or timestamp which will effect the query.
 * @param[in]	callback function which will process the result from the database
 * 		and update the server strctures.
 *
 * @return	int
 * @retval	0	- Success but no rows found
 * @retval	-1	- Failure
 * @retval	>0	- Success and number of rows found
 *
 */
int
pbs_db_search(void *conn, pbs_db_obj_info_t *obj, pbs_db_query_options_t *opts, query_cb_t query_cb)
{
	void *st;
	int ret;
	int totcount;

	st = db_initialize_state(conn, query_cb);
	if (!st)
		return -1;

	ret = db_fn_arr[obj->pbs_db_obj_type].pbs_db_find_obj(conn, st, obj, opts);
	if (ret == -1) {
		/* error in executing the sql */
		db_destroy_state(st);
		return -1;
	}
	totcount = ((db_query_state_t *)st)->count;
	db_destroy_state(st);
	return totcount;
}


/**
 * @brief
 *	Delete an existing object from the database
 *
 * @param[in]	conn - Connected database handle
 * @param[in]	pbs_db_obj_info_t - Wrapper object that describes the object
 *		(and data) to delete
 *
 * @return      int
 * @retval	-1  - Failure
 * @retval       0  - success
 * @retval	 1 -  Success but no rows deleted
 *
 */
int
pbs_db_delete_obj(void *conn, pbs_db_obj_info_t *obj)
{
	return 0;
	return (db_fn_arr[obj->pbs_db_obj_type].pbs_db_delete_obj(conn, obj));
}

/**
 * @brief
 *	Load a single existing object from the database
 *
 * @param[in]	conn - Connected database handle
 * @param[in/out]pbs_db_obj_info_t - Wrapper object that describes the object
 *		(and data) to load. This parameter used to return the data about
 *		the object loaded
 *
 * @return      Error code
 * @retval       0  - success
 * @retval	-1  - Failure
 * @retval	 1 -  Success but no rows loaded
 *
 */
int
pbs_db_load_obj(void *conn, pbs_db_obj_info_t *obj)
{
	return (db_fn_arr[obj->pbs_db_obj_type].pbs_db_load_obj(conn, obj));
}


/**
 * @brief
 *	Saves a new object into the database
 *
 * @param[in]	conn - Connected database handle
 * @param[in]	pbs_db_obj_info_t - Wrapper object that describes the object (and data) to insert
 * @param[in]	savetype - quick or full save
 *
 * @return      Error code
 * @retval	-1  - Failure
 * @retval	 0  - Success
 * @retval	 1  - Success but no rows inserted
 *
 */
int
pbs_db_save_obj(void *conn, pbs_db_obj_info_t *obj, int savetype)
{
	return (db_fn_arr[obj->pbs_db_obj_type].pbs_db_save_obj(conn, obj, savetype));
}

/**
 * @brief
 *	Delete attributes of an object from the database
 *
 * @param[in]	conn - Connected database handle
 * @param[in]	pbs_db_obj_info_t - Wrapper object that describes the object
 * @param[in]	id - Object id
 * @param[in]	attr_list - list of attributes to delete
 *
 * @return      Error code
 * @retval      0  - success
 * @retval     -1  - Failure
 *
 */
int
pbs_db_delete_attr_obj(void *conn, pbs_db_obj_info_t *obj, void *obj_id, pbs_db_attr_list_t *db_attr_list)
{
	return (db_fn_arr[obj->pbs_db_obj_type].pbs_db_del_attr_obj(conn, obj_id, db_attr_list));
}

/**
 * @brief
 *	Frees allocate memory of an Object
 *
 * @param[in]	obj - db object
 *
 * @return None
 *
 */
void
pbs_db_reset_obj(pbs_db_obj_info_t *obj)
{
	db_fn_arr[obj->pbs_db_obj_type].pbs_db_reset_obj(obj);
}

/**
 * @brief
 *	Check whether connection to pbs dataservice is fine
 *
 * @param[in]	conn - Connected database handle
 *
 * @return      Connection status
 * @retval      -1 - Connection down
 * @retval	 0 - Connection fine
 *
 */
int
pbs_db_is_conn_ok(void *conn)
{
	return 0;
}


/**
 * @brief
 *	Create a new connection structure and initialize the fields
 *
 * @param[out]  conn - Pointer to database connection handler.
 * @param[in]   host - The hostname to connect to
 * @param[in]	port - The port to connect to
 * @param[in]   timeout - The connection attempt timeout
 *
 * @return      int - failcode
 * @retval      non-zero  - Failure
 * @retval      0 - Success
 *
 */
int
pbs_db_connect(void **db_conn, char *host, int port, int timeout)
{
	const char DEFAULT_HOST[] = "127.0.0.1";
	const int DEFAULT_PORT = 3000;
	aerospike *p_as = &as;
	as_error err;
	int failcode = PBS_DB_SUCCESS;

	as_config config;
	as_config_init(&config);
	
	if (! as_config_add_hosts(&config, DEFAULT_HOST, DEFAULT_PORT)) {
		printf("Invalid host(s) %s\n", DEFAULT_HOST);
		exit(-1);
	}
	
	as_config_set_user(&config, NULL, NULL);
	config.auth_mode = AS_AUTH_INTERNAL;

	aerospike_init(p_as, &config);	

	if (aerospike_connect(p_as, &err) != AEROSPIKE_OK) {
		aerospike_destroy(p_as);
		failcode = PBS_DB_ERR;
	}

	/* Make a connection to the database */
	*db_conn = (void *) p_as;

	if (failcode != PBS_DB_SUCCESS)
		*db_conn = NULL;
	return failcode;
}

/**
 * @brief
 *	Disconnect from the database and frees all allocated memory.
 *
 * @param[in]   conn - Connected database handle
 *
 * @return      Error code
 * @retval       0  - success
 * @retval      -1  - Failure
 *
 */
int 
pbs_db_disconnect(void *conn)
{
	as_error err;
	aerospike *as = (aerospike *) conn;

	if (!as)
		return -1;

	// Disconnect from the database cluster and clean up the aerospike object.
	aerospike_destroy(as);
	aerospike_close(as, &err);
	//aerospike_destroy(as);

	return 0;
}


/**
 * @brief
 *	Function to set the database error into the db_err field of the
 *	connection object
 *
 * @param[in]	conn - Pointer to db connection handle.
 * @param[out]	conn_db_err - Pointer to cached db error.
 * @param[in]	fnc - Custom string added to the error message
 *			This can be used to provide the name of the functionality.
 * @param[in]	msg - Custom string added to the error message. This can be
 *			used to provide a failure message.
 * @param[in]	diag_msg - Additional diagnostic message from the resultset, if any
 */
void
db_set_error(char *fnc, char *msg, char *diag_msg)
{
	char fmt[] = "%s failed: %s %s";
	char **conn_db_err = &errmsg_cache;

	if (*conn_db_err) {
		free(*conn_db_err);
		*conn_db_err = NULL;
	}

	if (!diag_msg)
		diag_msg = "";

	pbs_asprintf(conn_db_err, fmt, fnc, msg, diag_msg);

#ifdef DEBUG
	printf("%s\n", errmsg_cache);
	fflush(stdout);
#endif
}





/**
 * @brief
 *	Function to start/stop the database service/daemons
 *	Basically calls the pbs_dataservice script/batch file with
 *	the specified command. It adds a second parameter
 *	"PBS" to the command string. This way the script/batch file
 *	knows that the call came from the pbs code rather than
 *	being invoked from commandline by the admin
 *
 * @return      Error code
 * @retval       !=0 - Failure
 * @retval         0 - Success
 *
 */
int
pbs_dataservice_control(char *cmd, char *pbs_ds_host, int pbs_ds_port)
{
	return 0;
}

/**
 * @brief
 *	Function to check whether data-service is running
 *
 * @return      Error code
 * @retval      -1  - Error in routine
 * @retval       0  - Data service running on local host
 * @retval       1  - Data service not running
 * @retval       2  - Data service running on another host
 *
 */
int
pbs_status_db(char *pbs_ds_host, int pbs_ds_port)
{
	return (pbs_dataservice_control(PBS_DB_CONTROL_STATUS, pbs_ds_host, pbs_ds_port));
}

/**
 * @brief
 *	Start the database daemons/service in synchronous mode.
 *  This function waits for the database to complete startup.
 *
 * @param[out]	errmsg - returns the startup error message if any
 *
 * @return       int
 * @retval       0     - success
 * @retval       !=0   - Failure
 *
 */
int
pbs_start_db(char *pbs_ds_host, int pbs_ds_port)
{
	return (pbs_dataservice_control(PBS_DB_CONTROL_START, pbs_ds_host, pbs_ds_port));
}

/**
 * @brief
 *	Function to stop the database service/daemons
 *	This passes the parameter STOP to the
 *	pbs_dataservice script.
 *
 * @param[out]	errmsg - returns the db error message if any
 *
 * @return      Error code
 * @retval       !=0 - Failure
 * @retval        0  - Success
 *
 */
int
pbs_stop_db(char *pbs_ds_host, int pbs_ds_port)
{
	return (pbs_dataservice_control(PBS_DB_CONTROL_STOP, pbs_ds_host, pbs_ds_port));
}

/**
 * @brief
 *	Function to escape special characters in a string
 *	before using as a column value in the database
 *
 * @param[in]	conn - Handle to the database connection
 * @param[in]	str - the string to escape
 *
 * @return      Escaped string
 * @retval        NULL - Failure to escape string
 * @retval       !NULL - Newly allocated area holding escaped string,
 *                       caller needs to free
 *
 */
char *
pbs_db_escape_str(void *conn, char *str)
{
	char *val_escaped;
	int val_len;

	if (str == NULL)
		return NULL;

	val_len = strlen(str);
	/* Use calloc() to ensure the character array is initialized. */
	val_escaped = calloc(((2*val_len) + 1), sizeof(char)); /* 2*orig + 1 as per aerospike API documentation */
	if (val_escaped == NULL)
		return NULL;

	return val_escaped;
}

/**
 * @brief
 *	Translates the error code to an error message
 *
 * @param[in]   err_code - Error code to translate
 * @param[out]   err_msg - The translated error message (newly allocated memory)
 *
 */
void
pbs_db_get_errmsg(int err_code, char **err_msg)
{
	if (*err_msg) {
		free(*err_msg);
		*err_msg = NULL;
	}

	switch (err_code) {
	case PBS_DB_STILL_STARTING:
		*err_msg = strdup("PBS dataservice is still starting up");
		break;

	case PBS_DB_AUTH_FAILED:
		*err_msg = strdup("PBS dataservice authentication failed");
		break;

	case PBS_DB_NOMEM:
		*err_msg = strdup("PBS out of memory in connect");
		break;

	case PBS_DB_CONNREFUSED:
		*err_msg = strdup("PBS dataservice not running");
		break;

	case PBS_DB_CONNFAILED:
		*err_msg = strdup("Failed to connect to PBS dataservice");
		break;

	case PBS_DB_OOM_ERR:
		*err_msg = strdup("Failed to protect PBS from Linux OOM killer. No access to OOM score file.");
		break;

	case PBS_DB_ERR:
		*err_msg = NULL;
		if (errmsg_cache)
			*err_msg = strdup(errmsg_cache);
		break;

	default:
		*err_msg = strdup("PBS dataservice error");
		break;
	}
}

/**
 * @brief convert network to host byte order to unsigned long long
 *
 * @param[in]   x - Value to convert
 *
 * @return Value converted from network to host byte order. Return the original
 * value if network and host byte order are identical.
 */
unsigned long long
pbs_ntohll(unsigned long long x)
{
	if (ntohl(1) == 1)
		return x;

	/*
	 * htonl and ntohl always work on 32 bits, even on a 64 bit platform,
	 * so there is no clash.
	 */
	return (unsigned long long)(((unsigned long long) ntohl((x) & 0xffffffff)) << 32) | ntohl(((unsigned long long)(x)) >> 32);
}

int
pbs_db_password(void *conn, char *userid, char *password, char *olduser)
{
	return 0;
}